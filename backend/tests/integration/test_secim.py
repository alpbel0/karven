"""Integration tests for the secimdagitimapp connector (isolated ``karven-test``).

The discovery command is exercised through its real loader path
(``_cmd_secim_discover`` -> ``_record_report`` -> ``upsert_discovered_codes`` /
``ensure_series`` / ``record_observations``) against the isolated test database;
only the network-facing connector is replaced by a fixture-backed stand-in.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.connectors.base import DatasetMeta, upsert_dataset, upsert_institution
from app.connectors.tuik.__main__ import _cmd_secim_discover
from app.connectors.tuik.secim import (
    DIM_CEVRE,
    DIM_ILCE,
    DIM_PARTI,
    DIM_YERLESIM,
    TABLE_SPEC_BY_INDEX,
    SecimConnector,
    WalkResult,
    _period_codes,
    interpret_report,
)
from app.connectors.tuik.zk import parse_report_grid
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    Institution,
    Observation,
    Series,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tuik" / "secim"
CEVRE = TABLE_SPEC_BY_INDEX[1]
PERIOD = date(2023, 5, 14)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _base_meta() -> DatasetMeta:
    """Real catalog metadata for table 1 with one party but not ``AK PARTİ``."""
    walk = WalkResult()
    for dimension, value in (
        (DIM_CEVRE, "Adana"),
        (DIM_ILCE, "Seyhan"),
        (DIM_YERLESIM, "İl/İlçe merkezi"),
    ):
        walk.add(dimension, value)
        walk.add_total(dimension)
    walk.add(DIM_PARTI, "CHP")
    walk.add_total(DIM_PARTI)
    return SecimConnector(store=None)._dataset_meta(CEVRE, walk)


def _meta_with_elections(labels: list[str]) -> DatasetMeta:
    """Table 1 metadata whose ``Yıllar`` list offered exactly ``labels``."""
    walk = WalkResult()
    walk.years = list(labels)
    walk.add(DIM_CEVRE, "Adana")
    walk.add_total(DIM_CEVRE)
    walk.add(DIM_PARTI, "CHP")
    walk.add_total(DIM_PARTI)
    return SecimConnector(store=None)._dataset_meta(CEVRE, walk)


def _fixture_series(dataset_code: str):
    grid = parse_report_grid((FIXTURES / "table1.report.html").read_bytes())
    series = interpret_report(grid, CEVRE, period=PERIOD)
    for entry in series:
        entry.dataset = dataset_code
    return series


class _FixtureConnector:
    """Fixture-backed stand-in for :class:`SecimConnector` (no network)."""

    institution_name = "TÜİK Seçim discover test"

    def __init__(self, plans, *, institution_code: str, fail_dataset: str | None = None) -> None:
        self.institution_code = institution_code
        self._plans = plans
        self._meta = _base_meta()
        self._fail = fail_dataset
        self.built: list[str] = []

    def discovery_plans(self, *, table=None, elections=None, catalog=None):
        return list(self._plans)

    def dataset_meta(self, dataset_code):
        return replace(self._meta, external_code=dataset_code)

    def build_report(self, plan):
        self.built.append(plan.dataset_code)
        if plan.dataset_code == self._fail:
            raise RuntimeError("boom")
        return _fixture_series(plan.dataset_code)


def _plan(dataset_code: str) -> SimpleNamespace:
    return SimpleNamespace(
        dataset_code=dataset_code,
        election="2023",
        period=PERIOD,
        spec=CEVRE,
    )


def _period_plan(dataset_code: str, election: str, period: date) -> SimpleNamespace:
    return SimpleNamespace(dataset_code=dataset_code, election=election, period=period, spec=CEVRE)


class _PeriodConnector(_FixtureConnector):
    """Like :class:`_FixtureConnector`, but retimes points to the plan period."""

    def build_report(self, plan):
        self.built.append(plan.dataset_code)
        if plan.dataset_code == self._fail:
            raise RuntimeError("boom")
        series = _fixture_series(plan.dataset_code)
        return [
            replace(entry, points=[(plan.period, value) for _period, value in entry.points])
            for entry in series
        ]


def _dataset_for(session, institution_code: str, dataset_code: str) -> Dataset | None:
    institution = session.scalar(sa.select(Institution).where(Institution.code == institution_code))
    if institution is None:
        return None
    return session.scalar(
        sa.select(Dataset).where(
            Dataset.institution_id == institution.id,
            Dataset.external_code == dataset_code,
        )
    )


def _observations_for(session, institution_code: str, dataset_code: str) -> int:
    dataset = _dataset_for(session, institution_code, dataset_code)
    if dataset is None:
        return 0
    return session.scalar(
        sa.select(sa.func.count())
        .select_from(Observation)
        .join(Series, Observation.series_id == Series.id)
        .where(Series.dataset_id == dataset.id)
    )


def _observations_at(session, institution_code: str, dataset_code: str, period: date) -> int:
    dataset = _dataset_for(session, institution_code, dataset_code)
    if dataset is None:
        return 0
    return session.scalar(
        sa.select(sa.func.count())
        .select_from(Observation)
        .join(Series, Observation.series_id == Series.id)
        .where(Series.dataset_id == dataset.id, Observation.period == period)
    )


def test_secim_discover_discovers_party_codes_and_writes_observations() -> None:
    institution_code = _unique("inst-secim")
    dataset_code = _unique("TUIK_SECIM_CEVRE_ILCE")
    connector = _FixtureConnector([_plan(dataset_code)], institution_code=institution_code)

    with SessionLocal() as session:
        assert (
            _cmd_secim_discover(session, connector, argparse.Namespace(table=None, elections=None))
            == 0
        )

    with SessionLocal() as session:
        # The report's 136 series (including 33 parties absent from the form list)
        # were loaded, and every observation was written.
        assert _observations_for(session, institution_code, dataset_code) == 136
        dataset = _dataset_for(session, institution_code, dataset_code)
        assert dataset is not None
        assert dataset.attributes["channel"] == "secimdagitimapp"

        dimension = session.scalar(
            sa.select(DatasetDimension).where(
                DatasetDimension.dataset_id == dataset.id,
                DatasetDimension.code == DIM_PARTI,
            )
        )
        assert dimension is not None
        codes = session.scalars(
            sa.select(DimensionCode).where(DimensionCode.dimension_id == dimension.id)
        ).all()
        by_code = {code.code: code for code in codes}
        assert {"_T", "CHP"} <= set(by_code)
        akp = by_code["AK PARTİ"]
        assert akp.attributes["discovered_from_report"] is True
        assert "first_seen" in akp.attributes


def test_secim_discover_commits_per_report_and_continues() -> None:
    institution_code = _unique("inst-secim-disc")
    plans = [_plan(_unique("TUIK_SECIM_DISC_A")), _plan(_unique("TUIK_SECIM_DISC_B"))]
    connector = _FixtureConnector(
        plans, institution_code=institution_code, fail_dataset=plans[1].dataset_code
    )

    with SessionLocal() as session:
        assert (
            _cmd_secim_discover(session, connector, argparse.Namespace(table=None, elections=None))
            == 0
        )

    with SessionLocal() as session:
        # The first report was committed before the second failed.
        assert _observations_for(session, institution_code, plans[0].dataset_code) == 136
        assert _observations_for(session, institution_code, plans[1].dataset_code) == 0


def test_secim_discover_skips_already_loaded_reports() -> None:
    institution_code = _unique("inst-secim-resume")
    plans = [_plan(_unique("TUIK_SECIM_RESUME"))]
    first = _FixtureConnector(plans, institution_code=institution_code)
    with SessionLocal() as session:
        assert (
            _cmd_secim_discover(session, first, argparse.Namespace(table=None, elections=None)) == 0
        )
    assert first.built == [plans[0].dataset_code]

    second = _FixtureConnector(plans, institution_code=institution_code)
    with SessionLocal() as session:
        assert (
            _cmd_secim_discover(session, second, argparse.Namespace(table=None, elections=None))
            == 0
        )
    assert second.built == []


def test_secim_discover_stores_both_2015_elections_as_separate_periods() -> None:
    institution_code = _unique("inst-secim-2015")
    dataset_code = _unique("TUIK_SECIM_ADAY_2015")
    november = date(2015, 11, 1)
    june = date(2015, 6, 7)
    plans = [
        _period_plan(dataset_code, "2015 seçimi (1 Kasım)", november),
        _period_plan(dataset_code, "2015 seçimi (7 Haziran)", june),
    ]
    args = argparse.Namespace(table=None, elections=None)

    first = _PeriodConnector(plans, institution_code=institution_code)
    with SessionLocal() as session:
        assert _cmd_secim_discover(session, first, args) == 0
    assert first.built == [dataset_code, dataset_code]

    with SessionLocal() as session:
        # Two distinct periods in the same year, not one collapsed Jan 1 value.
        assert _observations_at(session, institution_code, dataset_code, november) > 0
        assert _observations_at(session, institution_code, dataset_code, june) > 0
        assert _observations_at(session, institution_code, dataset_code, date(2015, 1, 1)) == 0

    # Resume keys on the exact date: both reports are already loaded.
    second = _PeriodConnector(plans, institution_code=institution_code)
    with SessionLocal() as session:
        assert _cmd_secim_discover(session, second, args) == 0
    assert second.built == []


def test_secim_discovered_party_code_survives_a_later_catalog_walk() -> None:
    institution_code = _unique("inst-secim-keep")
    dataset_code = _unique("TUIK_SECIM_CEVRE_ILCE")
    connector = _FixtureConnector([_plan(dataset_code)], institution_code=institution_code)
    with SessionLocal() as session:
        _cmd_secim_discover(session, connector, argparse.Namespace(table=None, elections=None))

    with SessionLocal() as session:
        institution = session.scalar(
            sa.select(Institution).where(Institution.code == institution_code)
        )
        dataset = _dataset_for(session, institution_code, dataset_code)
        assert dataset is not None
        dataset_id = dataset.id
        # A later catalog walk from the form cannot see "AK PARTİ"; it must stay.
        upsert_dataset(session, institution.id, replace(_base_meta(), external_code=dataset_code))
        session.commit()

    with SessionLocal() as session:
        code = session.scalar(
            sa.select(DimensionCode)
            .join(DatasetDimension, DimensionCode.dimension_id == DatasetDimension.id)
            .where(
                DatasetDimension.dataset_id == dataset_id,
                DatasetDimension.code == DIM_PARTI,
                DimensionCode.code == "AK PARTİ",
            )
        )
        assert code is not None
        assert not (code.attributes or {}).get("removed_at")
        assert code.attributes["discovered_from_report"] is True


def test_secim_catalog_replaces_time_period_with_the_tables_real_list() -> None:
    institution_code = _unique("inst-secim-periods")
    dataset_code = _unique("TUIK_SECIM_CEVRE_ILCE")
    # The previous catalog claimed every known election for every table.
    old_labels = [label for _code, label in _period_codes()]
    real_labels = [
        "2023",
        "2018",
        "2015 seçimi (7 Haziran)",
        "2015 seçimi (1 Kasım)",
        "2011 seçimi",
    ]

    with SessionLocal() as session:
        institution = upsert_institution(session, institution_code, "Seçim dönem testi")
        session.commit()
        upsert_dataset(
            session,
            institution.id,
            replace(_meta_with_elections(old_labels), external_code=dataset_code),
        )
        session.commit()
        # A later catalog walk offers only the table's real elections.
        upsert_dataset(
            session,
            institution.id,
            replace(_meta_with_elections(real_labels), external_code=dataset_code),
        )
        session.commit()

    with SessionLocal() as session:
        dataset = _dataset_for(session, institution_code, dataset_code)
        assert dataset is not None
        dimension = session.scalar(
            sa.select(DatasetDimension).where(
                DatasetDimension.dataset_id == dataset.id,
                DatasetDimension.code == "TIME_PERIOD",
            )
        )
        assert dimension is not None
        codes = session.scalars(
            sa.select(DimensionCode).where(DimensionCode.dimension_id == dimension.id)
        ).all()
        active = {code.code for code in codes if not (code.attributes or {}).get("removed_at")}
        removed = {code.code for code in codes if (code.attributes or {}).get("removed_at")}
        assert active == {
            "2011-06-12",
            "2015-06-07",
            "2015-11-01",
            "2018-06-24",
            "2023-05-14",
        }
        assert "1950-05-14" in removed
        assert len(removed) == len(old_labels) - len(real_labels)
        assert dataset.attributes["elections"] == real_labels
