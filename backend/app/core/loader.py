"""Core-series loader: fetch the registry, flag it, and link Turcat GDP rows.

The CLI (:mod:`app.core.__main__`) owns argument parsing, printing and exit
codes; this module owns the database work so it stays unit- and
integration-testable. ``load_one`` commits once per series, so an interruption
can never lose a finished series, and a failure is returned to the caller
instead of being logged and swallowed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.catalog.linking import (
    INDICATOR_DIMENSION,
    METHOD_MANUAL,
    RELATION_SAME_SERIES,
    STATUS_ACCEPTED,
    upsert_link,
)
from app.connectors.base import ConnectorError, ingest_series
from app.connectors.tcmb.connector import SERIE_DIMENSION, SOURCES, TcmbConnector
from app.core.registry import CORE_SERIES, GDP_DATASET, TURCAT_GDP_LINKS, CoreSeries
from app.data.errors import SeriesDefinitionError
from app.data.models import CatalogLink, Dataset, Institution, Observation, Series
from app.data.observations import get_latest

#: Core history starts here (DECISIONS §5).
DEFAULT_START = date(2000, 1, 1)

#: The Turcat sector dataset that carries the IMF SDDS GDP rows.
TURCAT_DATASET = "TURCAT_REEL"
TURCAT_INSTITUTION = "tuik"

ConnectorFor = Callable[[str], TcmbConnector]


# --- lookups ---------------------------------------------------------------


def find_institution(session: Session, code: str) -> Institution | None:
    return session.scalar(sa.select(Institution).where(Institution.code == code))


def find_dataset(session: Session, institution_code: str, external_code: str) -> Dataset | None:
    institution = find_institution(session, institution_code)
    if institution is None:
        return None
    return session.scalar(
        sa.select(Dataset).where(
            Dataset.institution_id == institution.id,
            Dataset.external_code == external_code,
        )
    )


def find_series(session: Session, institution_code: str, external_code: str) -> Series | None:
    institution = find_institution(session, institution_code)
    if institution is None:
        return None
    return session.scalar(
        sa.select(Series).where(
            Series.institution_id == institution.id,
            Series.external_code == external_code,
        )
    )


# --- load ------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesLoad:
    """Outcome of loading one core series (or fetching it in a dry run)."""

    external_code: str
    inserted: int
    unchanged: int
    point_count: int


@dataclass(frozen=True)
class LoadFailure:
    """A core series that could not be loaded."""

    external_code: str
    message: str


def select_core(only: str | None) -> list[CoreSeries]:
    """The registry, or the single entry ``only`` names (error if unknown)."""
    if only is None:
        return list(CORE_SERIES)
    selected = [core for core in CORE_SERIES if core.external_code == only]
    if not selected:
        raise ValueError(f"{only!r} is not a core series")
    return selected


def _dataset_missing_message(core: CoreSeries, institution_code: str) -> str:
    return (
        f"dataset {core.dataset_code!r} is not catalogued for institution "
        f"{institution_code!r}; run `python -m app.connectors.tcmb catalog "
        f"--source {core.source}` first"
    )


def load_one(
    session: Session,
    connector: TcmbConnector,
    core: CoreSeries,
    *,
    start: date = DEFAULT_START,
    dry_run: bool = False,
) -> SeriesLoad:
    """Fetch one core series, ingest it and flag it ``is_core`` (or dry-run).

    ``dry_run`` fetches and parses through the connector but writes nothing:
    no dataset lookup, no observations, no ``is_core``.
    """
    codes = {SERIE_DIMENSION: core.serie_code}
    if dry_run:
        fetched = connector.fetch_series(core.dataset_code, codes, start=start)
        return SeriesLoad(core.external_code, 0, 0, len(fetched.points))
    dataset = find_dataset(session, connector.institution_code, core.dataset_code)
    if dataset is None:
        raise LookupError(_dataset_missing_message(core, connector.institution_code))
    result = ingest_series(session, connector, dataset=dataset, codes=codes, start=start)
    series = session.get(Series, result.series_id)
    if series is None:  # pragma: no cover - ingest_series just created it
        raise LookupError(f"series {result.series_id} vanished during load")
    series.is_core = True
    # One commit per series: an interruption keeps every finished series.
    session.commit()
    return SeriesLoad(
        core.external_code,
        inserted=result.inserted,
        unchanged=result.unchanged,
        point_count=result.point_count,
    )


def load_core(
    session: Session,
    connector_for: ConnectorFor,
    *,
    start: date = DEFAULT_START,
    only: str | None = None,
    dry_run: bool = False,
) -> tuple[list[SeriesLoad], list[LoadFailure]]:
    """Load every selected core series; collect failures instead of stopping."""
    loads: list[SeriesLoad] = []
    failures: list[LoadFailure] = []
    for core in select_core(only):
        connector = connector_for(core.source)
        try:
            loads.append(load_one(session, connector, core, start=start, dry_run=dry_run))
        except (ConnectorError, SeriesDefinitionError, LookupError) as exc:
            session.rollback()
            failures.append(LoadFailure(core.external_code, str(exc)))
    return loads, failures


# --- Turcat links ----------------------------------------------------------


def rounds_equal(evds: Decimal | None, turcat: Decimal | None) -> bool:
    """True when ``evds`` rounds to the integer Turcat value ``turcat``."""
    if evds is None or turcat is None:
        return False
    return round(evds) == turcat


@dataclass(frozen=True)
class TurcatLink:
    """One verified (and, unless dry-run, accepted) Turcat -> EVDS link."""

    indicator: str
    serie_code: str
    periods: tuple[date, ...]


@dataclass(frozen=True)
class LinkFailure:
    """A Turcat -> EVDS pair that failed verification and was refused."""

    indicator: str
    serie_code: str
    message: str


def _quarter_label(period: date) -> str:
    return f"{period.year}Q{(period.month - 1) // 3 + 1}"


def link_one(
    session: Session,
    from_dataset: Dataset,
    to_dataset: Dataset,
    indicator: str,
    serie_code: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> TurcatLink:
    """Verify one Turcat indicator against its EVDS series and link it.

    Every Turcat period must have an EVDS observation for the same period with
    ``round(evds) == turcat``; otherwise a ``LookupError``/``ValueError`` is
    raised and nothing is linked.
    """
    turcat = find_series(session, TURCAT_INSTITUTION, f"{TURCAT_DATASET}:{indicator}")
    if turcat is None:
        raise LookupError(f"no Turcat series {TURCAT_DATASET}:{indicator}")
    evds = find_series(session, TURCAT_INSTITUTION, f"{GDP_DATASET}:{serie_code}")
    if evds is None:
        raise LookupError(f"no EVDS series {GDP_DATASET}:{serie_code}")

    turcat_rows = get_latest(session, turcat.id)
    if not turcat_rows:
        raise LookupError(f"no Turcat observations for {TURCAT_DATASET}:{indicator}")
    evds_values = {row.period: row.value for row in get_latest(session, evds.id)}

    periods: list[date] = []
    for row in turcat_rows:
        if row.period not in evds_values:
            raise ValueError(
                f"no EVDS {GDP_DATASET}:{serie_code} observation for {row.period.isoformat()}"
            )
        if not rounds_equal(evds_values[row.period], row.value):
            raise ValueError(
                f"Turcat {row.value} != round(EVDS {evds_values[row.period]}) "
                f"for {row.period.isoformat()}"
            )
        periods.append(row.period)

    note = (
        "GSYH A10 current prices; Turcat equals EVDS rounded for "
        + ", ".join(_quarter_label(period) for period in periods)
        + " (Task 1.4b)"
    )
    if not dry_run:
        link = upsert_link(
            session,
            from_dataset.id,
            {INDICATOR_DIMENSION: indicator},
            to_dataset.id,
            {SERIE_DIMENSION: serie_code},
            relation=RELATION_SAME_SERIES,
            method=METHOD_MANUAL,
            confidence=None,
            note=note,
        )
        if link.status != STATUS_ACCEPTED:
            link.status = STATUS_ACCEPTED
            link.reviewed_at = now or datetime.now(UTC)
            link.mapping = {}
        session.commit()
    return TurcatLink(indicator, serie_code, tuple(periods))


def link_turcat(
    session: Session, *, dry_run: bool = False, now: datetime | None = None
) -> tuple[list[TurcatLink], list[LinkFailure]]:
    """Link every Turcat GDP row that verifies against EVDS; refuse the rest."""
    from_dataset = find_dataset(session, TURCAT_INSTITUTION, TURCAT_DATASET)
    if from_dataset is None:
        raise LookupError(
            f"dataset {TURCAT_DATASET!r} is not catalogued; run the Turcat catalog first"
        )
    to_dataset = find_dataset(session, TURCAT_INSTITUTION, GDP_DATASET)
    if to_dataset is None:
        raise LookupError(f"dataset {GDP_DATASET!r} is not catalogued; run the core load first")

    links: list[TurcatLink] = []
    failures: list[LinkFailure] = []
    for indicator, serie_code in TURCAT_GDP_LINKS:
        try:
            links.append(
                link_one(
                    session,
                    from_dataset,
                    to_dataset,
                    indicator,
                    serie_code,
                    dry_run=dry_run,
                    now=now,
                )
            )
        except (LookupError, ValueError) as exc:
            session.rollback()
            failures.append(LinkFailure(indicator, serie_code, str(exc)))
    return links, failures


# --- status ----------------------------------------------------------------


@dataclass(frozen=True)
class CoreStatus:
    """One line of ``status``: is the series core and how much has landed."""

    external_code: str
    is_core: bool
    observation_count: int
    latest_period: date | None


def series_status(session: Session, core: CoreSeries) -> CoreStatus:
    institution_code = SOURCES[core.source].institution_code
    series = find_series(session, institution_code, core.external_code)
    if series is None:
        return CoreStatus(core.external_code, False, 0, None)
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(Observation)
        .where(Observation.series_id == series.id)
    )
    latest = session.scalar(
        sa.select(sa.func.max(Observation.period)).where(Observation.series_id == series.id)
    )
    return CoreStatus(core.external_code, bool(series.is_core), int(count or 0), latest)


def core_status(session: Session) -> list[CoreStatus]:
    return [series_status(session, core) for core in CORE_SERIES]


def turcat_gdp_link_count(session: Session) -> int:
    """How many Turcat -> GDP catalog links exist (0 until both are catalogued)."""
    from_dataset = find_dataset(session, TURCAT_INSTITUTION, TURCAT_DATASET)
    to_dataset = find_dataset(session, TURCAT_INSTITUTION, GDP_DATASET)
    if from_dataset is None or to_dataset is None:
        return 0
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(CatalogLink)
        .where(
            CatalogLink.from_dataset_id == from_dataset.id,
            CatalogLink.to_dataset_id == to_dataset.id,
        )
    )
    return int(count or 0)


__all__ = [
    "DEFAULT_START",
    "TURCAT_DATASET",
    "TURCAT_INSTITUTION",
    "ConnectorFor",
    "CoreStatus",
    "LinkFailure",
    "LoadFailure",
    "SeriesLoad",
    "TurcatLink",
    "core_status",
    "find_dataset",
    "find_institution",
    "find_series",
    "link_one",
    "link_turcat",
    "load_core",
    "load_one",
    "rounds_equal",
    "select_core",
    "series_status",
    "turcat_gdp_link_count",
]
