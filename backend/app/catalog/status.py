"""Series data status for agents (Task 2.5, DECISIONS §4 "agents never see values").

``series_loaded`` / ``loaded_ranges`` are the query-time "is this series loaded?"
helpers (Task 2.2 decision 3): whether a series has observations is derived from
the ``observations`` table, never stored on the dataset, so the answer stays exact
when values are fetched. Each helper runs one grouped query for all requested ids
(no N+1); the SQL is exposed separately so its shape is unit-testable.

The agent tool is :func:`build_status_tool` (``data_status``). It takes the recipes
``find_series`` returns (``institution`` + ``dataset`` + ``codes``; other keys are
ignored) and answers, per recipe: does the series exist, is it loaded, which
period range the source covers and which range we hold, frequency, unit, measure
and the transformations that make sense. **It never returns an observation
value**: only periods and counts leave the database.

Transformations are read from the catalog measure (Task 2.2 ``measure_combinations``),
never guessed from the unit: an unknown measure gives ``transforms: null``.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.catalog.search import measure_info
from app.connectors.base import build_series_definition
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset, DatasetDimension, Institution, Observation, Series

logger = logging.getLogger(__name__)

STATUS_TOOL_LIMIT = 3

LEVEL = "level"
PERCENT_CHANGE_PERIOD = "percent_change_period"
PERCENT_CHANGE_ANNUAL = "percent_change_annual"

# Measures whose level is a quantity, so a percent change of it is meaningful.
# Everything else (already a % change, a rate, a share, a weight, an index of
# answers, a distribution statistic...) is level-only.
_PERCENT_CHANGE_MEASURES = frozenset(
    {
        "akim_tutar",
        "stok_tutar",
        "akim_adet",
        "stok_adet",
        "fiziksel_akim",
        "fiziksel_stok",
        "fiyat_kur",
        "endeks",
        "kisi_basina",
    }
)

# Frequencies with a "same period last year" comparison; annual has only the
# previous-period change (which is the yearly change); biennial/irregular have none.
_ANNUAL_COMPARE_FREQUENCIES = frozenset({"monthly", "quarterly", "semiannual"})
_PERIOD_CHANGE_FREQUENCIES = frozenset(
    {"daily", "weekly", "monthly", "quarterly", "semiannual", "annual"}
)


# --------------------------------------------------------------------------- #
# Loaded / range queries
# --------------------------------------------------------------------------- #


def loaded_series_query(series_ids: Iterable[int]) -> Select[tuple[int]]:
    """The single grouped query behind :func:`series_loaded`."""
    return (
        select(Observation.series_id)
        .where(Observation.series_id.in_(list(series_ids)))
        .group_by(Observation.series_id)
    )


def series_loaded(session: Session, series_ids: Iterable[int]) -> dict[int, bool]:
    """Map each series id to whether it has at least one observation.

    Returns an entry for every requested id (``False`` when it has none), so a
    caller can zip ids and results without a second lookup.
    """
    ids = list(series_ids)
    if not ids:
        return {}
    loaded = set(session.execute(loaded_series_query(ids)).scalars().all())
    return {series_id: series_id in loaded for series_id in ids}


def loaded_ranges_query(series_ids: Iterable[int]) -> Select[tuple[int, date, date, int]]:
    """One grouped query: first period, last period and period count per series.

    Only ``period`` is selected (never ``value``), and ``count(distinct period)``
    counts periods, not re-fetched rows.
    """
    return (
        select(
            Observation.series_id,
            func.min(Observation.period),
            func.max(Observation.period),
            func.count(sa.distinct(Observation.period)),
        )
        .where(Observation.series_id.in_(list(series_ids)))
        .group_by(Observation.series_id)
    )


def loaded_ranges(session: Session, series_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Map each series id *that has observations* to its loaded period range."""
    ids = list(series_ids)
    if not ids:
        return {}
    rows = session.execute(loaded_ranges_query(ids)).all()
    return {
        series_id: {"start": first.isoformat(), "end": last.isoformat(), "periods": int(count)}
        for series_id, first, last, count in rows
    }


# --------------------------------------------------------------------------- #
# Transformations
# --------------------------------------------------------------------------- #


def allowed_transforms(measure: Mapping[str, Any] | None, frequency: str) -> list[str] | None:
    """The transformations that make sense for a series, or ``None`` when unknown.

    ``level`` is always allowed once the measure is known. Percent changes are
    offered only for quantity measures (not for a series that already is a %
    change, a rate or a share), not for cumulative (year-to-date) series, and only
    for frequencies that have a previous period / same period last year.
    """
    if not measure or not measure.get("measure_type"):
        return None
    transforms = [LEVEL]
    if measure.get("kumulatif") or measure["measure_type"] not in _PERCENT_CHANGE_MEASURES:
        return transforms
    if frequency in _PERIOD_CHANGE_FREQUENCIES:
        transforms.append(PERCENT_CHANGE_PERIOD)
    if frequency in _ANNUAL_COMPARE_FREQUENCIES:
        transforms.append(PERCENT_CHANGE_ANNUAL)
    return transforms


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def _range(start: date | None, end: date | None) -> dict[str, str | None] | None:
    if start is None and end is None:
        return None
    return {
        "start": start.isoformat() if start is not None else None,
        "end": end.isoformat() if end is not None else None,
    }


def _invalid(item: Any, message: str) -> dict[str, Any]:
    return {"status": "invalid_request", "message": message, "request": _echo(item)}


def _echo(item: Any) -> Any:
    if not isinstance(item, Mapping):
        return None
    return {key: item.get(key) for key in ("institution", "dataset", "codes")}


def _parse_item(item: Any) -> tuple[str, str, dict[str, str]] | str:
    """``(institution, dataset, codes)`` or an error message."""
    if not isinstance(item, Mapping):
        return "each recipe must be an object with institution, dataset and codes"
    institution = item.get("institution")
    dataset = item.get("dataset")
    codes = item.get("codes")
    if not isinstance(institution, str) or not institution:
        return "institution must be a non-empty string"
    if not isinstance(dataset, str) or not dataset:
        return "dataset must be a non-empty string"
    if not isinstance(codes, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in codes.items()
    ):
        return "codes must be an object of string dimension code -> string item code"
    return institution, dataset, {str(key): str(value) for key, value in codes.items()}


def _load_dataset(session: Session, institution_code: str, dataset_code: str) -> Dataset | None:
    return session.scalars(
        select(Dataset)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(Institution.code == institution_code, Dataset.external_code == dataset_code)
        .options(
            selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes),
            selectinload(Dataset.measure_combinations),
        )
    ).first()


def data_status(session: Session, items: Sequence[Any]) -> dict[str, Any]:
    """Status of up to :data:`STATUS_TOOL_LIMIT` recipes, in input order. Read-only."""
    selected = list(items)[:STATUS_TOOL_LIMIT]
    skipped = len(items) - len(selected)

    resolved: list[tuple[Any, tuple[str, str, dict[str, str]] | str]] = [
        (item, _parse_item(item)) for item in selected
    ]

    # Pass 1: dataset + definition per item; collect the series ids that exist.
    prepared: list[dict[str, Any]] = []
    for item, parsed in resolved:
        if isinstance(parsed, str):
            prepared.append({"result": _invalid(item, parsed)})
            continue
        institution_code, dataset_code, codes = parsed
        base = {"institution": institution_code, "dataset": dataset_code, "codes": codes}
        dataset = _load_dataset(session, institution_code, dataset_code)
        if dataset is None:
            prepared.append(
                {
                    "result": {
                        **base,
                        "status": "unknown_dataset",
                        "message": "no such dataset in the catalog",
                    }
                }
            )
            continue
        if dataset.veri_yok:
            prepared.append(
                {
                    "result": {
                        **base,
                        "status": "veri_yok",
                        "message": "the source lists this dataset but publishes no data for it",
                        "loaded": False,
                    }
                }
            )
            continue
        try:
            definition = build_series_definition(dataset, codes)
        except SeriesDefinitionError as exc:
            prepared.append({"result": {**base, "status": "invalid_codes", "message": str(exc)}})
            continue
        series = session.scalars(
            select(Series).where(
                Series.institution_id == dataset.institution_id,
                Series.external_code == definition.external_code,
            )
        ).first()
        prepared.append(
            {
                "base": base,
                "dataset": dataset,
                "definition": definition,
                "series": series,
            }
        )

    series_ids = [entry["series"].id for entry in prepared if entry.get("series") is not None]
    ranges = loaded_ranges(session, series_ids)

    results: list[dict[str, Any]] = []
    for entry in prepared:
        if "result" in entry:
            results.append(entry["result"])
            continue
        dataset = entry["dataset"]
        definition = entry["definition"]
        series = entry["series"]
        codes = entry["base"]["codes"]
        loaded_range = ranges.get(series.id) if series is not None else None
        # ``series.coverage_*`` only widens as observations are loaded, so the range
        # the source offers always comes from the dataset; what we hold comes from
        # the observations.
        available = _range(dataset.coverage_start, dataset.coverage_end)
        frequency = series.frequency if series is not None else definition.frequency
        unit = series.unit if series is not None else definition.unit
        measure = measure_info(dataset, codes)
        results.append(
            {
                **entry["base"],
                "status": "ok",
                "series_external_code": definition.external_code,
                "series_name": definition.name,
                "series_exists": series is not None,
                "series_id": series.id if series is not None else None,
                "loaded": loaded_range is not None,
                "frequency": frequency,
                "unit": unit,
                "available_range": available,
                "loaded_range": loaded_range,
                "measure": measure,
                "transforms": allowed_transforms(measure, frequency),
                "arsiv": bool(dataset.arsiv),
            }
        )

    output: dict[str, Any] = {"results": results}
    if skipped > 0:
        output["skipped"] = skipped
        output["note"] = (
            f"only the first {STATUS_TOOL_LIMIT} recipes were checked; {skipped} skipped"
        )
    return output


# --------------------------------------------------------------------------- #
# Agent tool + CLI
# --------------------------------------------------------------------------- #


def build_status_tool(session_factory: Any) -> dict[str, tuple[dict[str, Any], Any]]:
    """The ``data_status`` agent tool (``{name: (schema, function)}``)."""

    def tool_data_status(recipes: list[Any]) -> dict[str, Any]:
        if not isinstance(recipes, list) or not recipes:
            return {"status": "invalid_request", "message": "recipes must be a non-empty list"}
        with session_factory() as session:
            return data_status(session, recipes)

    schema = {
        "type": "object",
        "description": (
            "Data status of series found by find_series. Pass the find_series recipes "
            "(institution, dataset, codes; at most 3 per call) and get, per recipe, "
            "whether the series is loaded, the period range the source covers and the "
            "range we hold, frequency, unit, measure and the transformations that make "
            "sense (level, percent_change_period, percent_change_annual; null when the "
            "measure is unknown). Never returns values."
        ),
        "properties": {
            "recipes": {
                "type": "array",
                "maxItems": STATUS_TOOL_LIMIT,
                "items": {
                    "type": "object",
                    "properties": {
                        "institution": {"type": "string"},
                        "dataset": {"type": "string"},
                        "codes": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                    "required": ["institution", "dataset", "codes"],
                },
            }
        },
        "required": ["recipes"],
    }
    return {"data_status": (schema, tool_data_status)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.status",
        description="Show the data status of series recipes (Task 2.5).",
    )
    parser.add_argument("institution", help="institution code, e.g. TUIK")
    parser.add_argument("dataset", help="dataset external code")
    parser.add_argument(
        "codes",
        help='dimension codes as JSON, e.g. \'{"FREQ": "M", "TR": "TR"}\'',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        codes = json.loads(args.codes)
    except json.JSONDecodeError as exc:
        print(f"codes is not valid JSON: {exc}")
        return 2
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        result = data_status(
            session, [{"institution": args.institution, "dataset": args.dataset, "codes": codes}]
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    first = result["results"][0]["status"]
    return 0 if first == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LEVEL",
    "PERCENT_CHANGE_ANNUAL",
    "PERCENT_CHANGE_PERIOD",
    "STATUS_TOOL_LIMIT",
    "allowed_transforms",
    "build_status_tool",
    "data_status",
    "loaded_ranges",
    "loaded_ranges_query",
    "loaded_series_query",
    "series_loaded",
]
