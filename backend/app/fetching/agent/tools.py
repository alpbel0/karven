"""Read-only, value-free tools for the data-fetch agent (Task 1.6).

Every tool returns metadata only: names, codes, frequency, unit, coverage dates,
observation counts and job status/reasons. Observation values are never read
into a tool result. All tools are read-only and every result is capped, so a
single call can never flood the model.

The functions take a :class:`~sqlalchemy.orm.Session` so they are directly unit
testable; :func:`build_tools` binds them to a session factory for
``run_tool_loop`` in the shape ``{name: (json_schema, function)}``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.connectors.base import ROLE_TIME, build_series_definition
from app.core.loader import find_dataset, find_institution, find_series
from app.data.models import Dataset, FetchJob, Institution, Observation, Series

SessionFactory = Callable[[], Session]

MAX_SEARCH_RESULTS = 20
MAX_DIMENSION_CODES = 50
MAX_JOB_HISTORY = 10


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _institutions(session: Session, institution: str | None) -> list[Institution]:
    if institution is None:
        return list(session.scalars(sa.select(Institution)).all())
    row = find_institution(session, institution)
    if row is None:
        raise ValueError(f"institution {institution!r} is not catalogued")
    return [row]


def search_catalog(
    session: Session,
    *,
    query: str,
    institution: str | None = None,
    limit: int = MAX_SEARCH_RESULTS,
) -> list[dict[str, Any]]:
    """Case-insensitive search over dataset name/code and series name."""
    cap = max(1, min(int(limit or MAX_SEARCH_RESULTS), MAX_SEARCH_RESULTS))
    pattern = f"%{(query or '').strip()}%"
    rows: list[dict[str, Any]] = []
    for inst in _institutions(session, institution):
        datasets = session.scalars(
            sa.select(Dataset).where(
                Dataset.institution_id == inst.id,
                sa.or_(
                    Dataset.name.ilike(pattern),
                    Dataset.external_code.ilike(pattern),
                ),
            )
        ).all()
        for dataset in datasets:
            rows.append(
                {
                    "institution": inst.code,
                    "dataset": dataset.external_code,
                    "name": dataset.name,
                    "frequency": (dataset.attributes or {}).get("default_frequency"),
                    "coverage_start": _iso(dataset.coverage_start),
                    "coverage_end": _iso(dataset.coverage_end),
                }
            )
            if len(rows) >= cap:
                return rows[:cap]
        series_rows = session.scalars(
            sa.select(Series).where(
                Series.institution_id == inst.id,
                sa.or_(
                    Series.name.ilike(pattern),
                    Series.external_code.ilike(pattern),
                ),
            )
        ).all()
        for series in series_rows:
            dataset = session.get(Dataset, series.dataset_id)
            rows.append(
                {
                    "institution": inst.code,
                    "dataset": dataset.external_code if dataset is not None else None,
                    "name": series.name,
                    "frequency": series.frequency,
                    "coverage_start": _iso(series.coverage_start),
                    "coverage_end": _iso(series.coverage_end),
                }
            )
            if len(rows) >= cap:
                return rows[:cap]
    return rows[:cap]


def get_dataset_meta(
    session: Session,
    *,
    institution: str,
    dataset: str,
    dimension: str | None = None,
    code_query: str | None = None,
) -> dict[str, Any]:
    """Dataset name, channel, coverage, dimensions and (optionally) codes."""
    row = find_dataset(session, institution, dataset)
    if row is None:
        raise ValueError(f"dataset {dataset!r} is not catalogued for institution {institution!r}")
    attributes = row.attributes or {}
    dimensions = sorted(row.dimensions, key=lambda dim: dim.position)
    result: dict[str, Any] = {
        "institution": institution,
        "dataset": row.external_code,
        "name": row.name,
        "channel": attributes.get("channel"),
        "coverage_start": _iso(row.coverage_start),
        "coverage_end": _iso(row.coverage_end),
        "dimensions": [
            {
                "code": dim.code,
                "name": dim.label,
                "position": dim.position,
                "is_time": dim.role == ROLE_TIME,
            }
            for dim in dimensions
        ],
        "codes": None,
    }
    if dimension is not None:
        target = next((dim for dim in dimensions if dim.code == dimension), None)
        if target is None:
            raise ValueError(f"dimension {dimension!r} is not part of dataset {dataset!r}")
        codes = [code for code in target.codes if not (code.attributes or {}).get("removed_at")]
        if code_query:
            needle = code_query.lower()
            codes = [
                code
                for code in codes
                if needle in code.code.lower() or needle in code.label.lower()
            ]
        result["codes"] = [
            {"code": code.code, "label": code.label} for code in codes[:MAX_DIMENSION_CODES]
        ]
    return result


#: Returned by ``get_series_meta`` when no ``series`` row exists yet, so the
#: agent never reads lazy creation as "the data is missing at the source".
MISSING_SERIES_NOTE = (
    "No series row yet: series rows are created on the first successful fetch, "
    "so this does NOT mean the data is missing at the source. Judge the series "
    "by the dataset's codelist (get_dataset_meta)."
)


def _dimension_codelists(dataset: Dataset) -> dict[str, set[str]]:
    """Active code sets per non-time dimension of ``dataset``."""
    return {
        dimension.code: {
            code.code for code in dimension.codes if not (code.attributes or {}).get("removed_at")
        }
        for dimension in dataset.dimensions
        if dimension.role != ROLE_TIME
    }


def _invalid_codes(dataset: Dataset, codes: dict[str, str]) -> list[str]:
    """The given codes that are not in their dimension's codelist."""
    codelists = _dimension_codelists(dataset)
    return [
        code
        for dimension_code, code in codes.items()
        if code not in codelists.get(dimension_code, set())
    ]


def get_series_meta(
    session: Session,
    *,
    institution: str,
    dataset: str,
    codes: dict[str, str],
) -> dict[str, Any]:
    """Whether the series row exists, its metadata and whether the codes are valid.

    A missing row is not evidence of absence (rows are created lazily); the
    ``note`` says so and ``codes_valid``/``invalid_codes`` let the agent base a
    ``bad_request`` on the dataset's codelist, not on the missing row.
    """
    row = find_dataset(session, institution, dataset)
    if row is None:
        raise ValueError(f"dataset {dataset!r} is not catalogued for institution {institution!r}")
    provided = set(codes)
    expected = set(_dimension_codelists(row))
    invalid_codes = _invalid_codes(row, codes)
    codes_valid = not invalid_codes and provided == expected
    external_code: str | None = None
    if codes_valid:
        external_code = build_series_definition(row, codes).external_code
    series = find_series(session, institution, external_code) if external_code is not None else None
    if series is None:
        result: dict[str, Any] = {
            "exists": False,
            "codes_valid": codes_valid,
            "invalid_codes": invalid_codes,
            "note": MISSING_SERIES_NOTE,
        }
        if external_code is not None:
            result["external_code"] = external_code
        return result
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(Observation)
        .where(Observation.series_id == series.id)
    )
    return {
        "exists": True,
        "external_code": series.external_code,
        "name": series.name,
        "unit": series.unit,
        "frequency": series.frequency,
        "coverage_start": _iso(series.coverage_start),
        "coverage_end": _iso(series.coverage_end),
        "observation_count": int(count or 0),
        "codes_valid": codes_valid,
        "invalid_codes": invalid_codes,
    }


def get_job_history(
    session: Session,
    *,
    institution: str,
    external_code: str,
    limit: int = MAX_JOB_HISTORY,
) -> list[dict[str, Any]]:
    """Past fetch jobs for one series, newest first (metadata only)."""
    inst = find_institution(session, institution)
    if inst is None:
        raise ValueError(f"institution {institution!r} is not catalogued")
    cap = max(1, min(int(limit or MAX_JOB_HISTORY), MAX_JOB_HISTORY))
    jobs = session.scalars(
        sa.select(FetchJob)
        .where(
            FetchJob.institution_id == inst.id,
            FetchJob.external_code == external_code,
        )
        .order_by(FetchJob.id.desc())
    ).all()
    return [
        {
            "job_id": job.id,
            "status": job.status,
            "reason": job.error_reason,
            "origin": (job.attributes or {}).get("origin"),
            "agent_round": (job.attributes or {}).get("agent_round"),
            "requested_at": _iso(job.requested_at),
            "finished_at": _iso(job.finished_at),
        }
        for job in jobs[:cap]
    ]


def _schema(description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": required,
    }


ToolMap = dict[str, tuple[dict[str, Any], Callable[..., Any]]]


def build_tools(session_factory: SessionFactory) -> ToolMap:
    """Bind the read-only tools to ``session_factory`` for ``run_tool_loop``."""

    def tool_search_catalog(
        query: str, institution: str | None = None, limit: int = MAX_SEARCH_RESULTS
    ) -> list[dict[str, Any]]:
        with session_factory() as session:
            return search_catalog(session, query=query, institution=institution, limit=limit)

    def tool_get_dataset_meta(
        institution: str,
        dataset: str,
        dimension: str | None = None,
        code_query: str | None = None,
    ) -> dict[str, Any]:
        with session_factory() as session:
            return get_dataset_meta(
                session,
                institution=institution,
                dataset=dataset,
                dimension=dimension,
                code_query=code_query,
            )

    def tool_get_series_meta(
        institution: str, dataset: str, codes: dict[str, str]
    ) -> dict[str, Any]:
        with session_factory() as session:
            return get_series_meta(session, institution=institution, dataset=dataset, codes=codes)

    def tool_get_job_history(
        institution: str, external_code: str, limit: int = MAX_JOB_HISTORY
    ) -> list[dict[str, Any]]:
        with session_factory() as session:
            return get_job_history(
                session,
                institution=institution,
                external_code=external_code,
                limit=limit,
            )

    return {
        "search_catalog": (
            _schema(
                "Search the catalog (dataset names/codes and series names) with a "
                "case-insensitive substring. Returns metadata rows only.",
                {
                    "query": {"type": "string"},
                    "institution": {"type": ["string", "null"]},
                    "limit": {"type": "integer"},
                },
                ["query"],
            ),
            tool_search_catalog,
        ),
        "get_dataset_meta": (
            _schema(
                "Dataset metadata: name, channel, coverage, dimensions. With a "
                "dimension, up to 50 of its codes (optionally filtered by "
                "code_query).",
                {
                    "institution": {"type": "string"},
                    "dataset": {"type": "string"},
                    "dimension": {"type": ["string", "null"]},
                    "code_query": {"type": ["string", "null"]},
                },
                ["institution", "dataset"],
            ),
            tool_get_dataset_meta,
        ),
        "get_series_meta": (
            _schema(
                "Whether a series (dataset plus one code per non-time dimension) "
                "exists, with name, unit, frequency, coverage and observation "
                "count, plus codes_valid/invalid_codes checked against the "
                "dataset's codelist. A missing series row (exists=false) is NOT "
                "evidence that the data is absent: rows are created on the first "
                "successful fetch; judge the request by the codelist. Never "
                "returns values.",
                {
                    "institution": {"type": "string"},
                    "dataset": {"type": "string"},
                    "codes": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                ["institution", "dataset", "codes"],
            ),
            tool_get_series_meta,
        ),
        "get_job_history": (
            _schema(
                "Recent fetch jobs for one series: status, reason, origin, agent_round and times.",
                {
                    "institution": {"type": "string"},
                    "external_code": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["institution", "external_code"],
            ),
            tool_get_job_history,
        ),
    }


__all__ = [
    "MAX_DIMENSION_CODES",
    "MAX_JOB_HISTORY",
    "MAX_SEARCH_RESULTS",
    "build_tools",
    "get_dataset_meta",
    "get_job_history",
    "get_series_meta",
    "search_catalog",
]
