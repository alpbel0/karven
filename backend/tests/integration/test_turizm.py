"""Integration tests for the turizmapp connector (isolated ``karven-test``)."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.connectors.base import (
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    ensure_series,
    ingest_series,
    resolve_external_code,
    upsert_dataset,
    upsert_discovered_codes,
    upsert_institution,
)
from app.connectors.tuik.__main__ import _cmd_turizm_discover
from app.connectors.tuik.turizm import FORM_PATH_BY_CODE, InterpretedSeries, ReportPlan, WalkResult
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    Institution,
    Observation,
    Series,
)
from app.data.observations import get_latest, record_observations
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _small_meta(external_code: str) -> DatasetMeta:
    """A tiny monthly border dataset with one nationality and gate code."""
    return DatasetMeta(
        external_code=external_code,
        name="TÜİK Turizm test dataset",
        source_category="Turizm İstatistikleri",
        attributes={
            "channel": "turizmapp",
            "page": "sinir",
            "default_frequency": "monthly",
        },
        dimensions=[
            DimensionMeta(
                code="MILLIYET",
                label="Milliyet",
                position=0,
                role=ROLE_GEO,
                codes=[
                    DimensionCodeMeta(code="_T", label="Toplam", is_default=True),
                    DimensionCodeMeta(code="Almanya", label="Almanya"),
                ],
            ),
            DimensionMeta(
                code="KAPI",
                label="Kapı",
                position=1,
                role=ROLE_OTHER,
                codes=[
                    DimensionCodeMeta(code="_T", label="Toplam", is_default=True),
                    DimensionCodeMeta(code="Esenboğa", label="Esenboğa"),
                ],
            ),
            DimensionMeta(code="TIME_PERIOD", label="Dönem", position=2, role=ROLE_TIME, codes=[]),
        ],
    )


def test_turizm_catalog_upsert_writes_dimensions_and_codes() -> None:
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "Turizm test")
        meta = _small_meta(_unique("TUIK_TURIZM_TEST"))
        outcome, dataset = upsert_dataset(session, institution.id, meta)
        session.commit()
        assert outcome == "inserted"
        dataset_id = dataset.id
        external_code = dataset.external_code

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.attributes["channel"] == "turizmapp"
        assert dataset.attributes["default_frequency"] == "monthly"
        dimensions = {
            dimension.code: dimension
            for dimension in session.scalars(
                sa.select(DatasetDimension).where(DatasetDimension.dataset_id == dataset_id)
            )
        }
        assert set(dimensions) == {"MILLIYET", "KAPI", "TIME_PERIOD"}
        codes = session.scalars(
            sa.select(DimensionCode.code).where(DimensionCode.dimension_id == dimensions["KAPI"].id)
        ).all()
        assert set(codes) == {"_T", "Esenboğa"}
        assert dataset.external_code == external_code


def test_turizm_headline_series_is_recorded_and_read_back() -> None:
    external_code = _unique("TUIK_TURIZM_HEAD")
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "Turizm headline")
        _, dataset = upsert_dataset(session, institution.id, _small_meta(external_code))
        session.commit()
        dataset_id = dataset.id

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        codes = {"MILLIYET": "_T", "KAPI": "_T"}
        series = ensure_series(session, dataset, codes)
        points = [
            (date(2025, 1, 1), Decimal("2171118")),
            (date(2025, 2, 1), Decimal("2171942")),
        ]
        record_observations(
            session,
            series.id,
            points,
            fetched_at=datetime.now(UTC),
            raw_object_key="sources/tuik/turizmapp/report.html",
        )
        session.commit()
        series_id = series.id
        assert series.frequency == "monthly"
        assert series.external_code == f"{external_code}:_T._T"

    with SessionLocal() as session:
        latest = get_latest(session, series_id)
        assert [row.period for row in latest] == [date(2025, 1, 1), date(2025, 2, 1)]
        assert latest[0].value == Decimal("2171118")


def _report_meta(external_code: str) -> DatasetMeta:
    """A tiny income-style dataset whose categories come only from reports."""
    return DatasetMeta(
        external_code=external_code,
        name="TÜİK Turizm report test dataset",
        source_category="Turizm İstatistikleri",
        attributes={
            "channel": "turizmapp",
            "page": "cikis",
            "default_frequency": "quarterly",
            "variable_report": True,
        },
        dimensions=[
            DimensionMeta(
                code="VARIABLE",
                label="Turizm değişkeni",
                position=0,
                role=ROLE_OTHER,
                codes=[
                    DimensionCodeMeta(code="_T", label="Toplam", is_default=True),
                    DimensionCodeMeta(code="Milliyet", label="Milliyet"),
                ],
            ),
            DimensionMeta(
                code="KATEGORI",
                label="Kırılım",
                position=1,
                role=ROLE_OTHER,
                codes=[DimensionCodeMeta(code="_T", label="Toplam", is_default=True)],
            ),
            DimensionMeta(code="TIME_PERIOD", label="Dönem", position=2, role=ROLE_TIME, codes=[]),
        ],
    )


def test_turizm_discovered_report_code_survives_a_catalog_walk() -> None:
    external_code = _unique("TUIK_TURIZM_REPORT")
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "Turizm report")
        _, dataset = upsert_dataset(session, institution.id, _report_meta(external_code))
        session.commit()
        dataset_id = dataset.id
        institution_id = institution.id

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        added = upsert_discovered_codes(
            session, dataset, {"VARIABLE": "Milliyet", "KATEGORI": "Almanya"}
        )
        assert added == {"KATEGORI": 1}
        series = ensure_series(session, dataset, {"VARIABLE": "Milliyet", "KATEGORI": "Almanya"})
        record_observations(
            session,
            series.id,
            [(date(2012, 1, 1), Decimal("1000"))],
            fetched_at=datetime.now(UTC),
        )
        session.commit()

    with SessionLocal() as session:
        dimension = session.scalar(
            sa.select(DatasetDimension).where(
                DatasetDimension.dataset_id == dataset_id,
                DatasetDimension.code == "KATEGORI",
            )
        )
        assert dimension is not None
        code = session.scalar(
            sa.select(DimensionCode).where(
                DimensionCode.dimension_id == dimension.id, DimensionCode.code == "Almanya"
            )
        )
        assert code is not None
        assert code.attributes["discovered_from_report"] is True
        assert "first_seen" in code.attributes

    with SessionLocal() as session:
        # A later catalog walk cannot see "Almanya"; it must not mark it removed.
        upsert_dataset(session, institution_id, _report_meta(external_code))
        session.commit()

    with SessionLocal() as session:
        code = session.scalar(
            sa.select(DimensionCode)
            .join(DatasetDimension, DimensionCode.dimension_id == DatasetDimension.id)
            .where(
                DatasetDimension.dataset_id == dataset_id,
                DatasetDimension.code == "KATEGORI",
                DimensionCode.code == "Almanya",
            )
        )
        assert code is not None
        assert not (code.attributes or {}).get("removed_at")
        assert code.attributes["discovered_from_report"] is True


class _DiscoveringFetchConnector:
    """Fake connector that exposes an on-demand report-only code."""

    institution_code = "inst-turizm-ingest"
    channel = "turizmapp"

    def fetch_series(self, dataset_code, codes, *, order=None, start=date(2000, 1, 1)):
        return FetchResult(
            external_code=dataset_code,
            points=[(date(2012, 1, 1), Decimal("7"))],
            raw_object_keys=[],
            channel=self.channel,
        )

    def register_discovered_codes(self, session, dataset, codes) -> None:
        upsert_discovered_codes(session, dataset, codes)


def test_ingest_series_registers_report_codes_before_validation() -> None:
    external_code = _unique("TUIK_TURIZM_INGEST")
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "Turizm ingest")
        _, dataset = upsert_dataset(session, institution.id, _report_meta(external_code))
        session.commit()
        dataset_id = dataset.id

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        result = ingest_series(
            session,
            _DiscoveringFetchConnector(),
            dataset=dataset,
            codes={"VARIABLE": "Milliyet", "KATEGORI": "Almanya"},
        )
        session.commit()

    assert result.point_count == 1

    with SessionLocal() as session:
        code = session.scalar(
            sa.select(DimensionCode)
            .join(DatasetDimension, DimensionCode.dimension_id == DatasetDimension.id)
            .where(
                DatasetDimension.dataset_id == dataset_id,
                DatasetDimension.code == "KATEGORI",
                DimensionCode.code == "Almanya",
            )
        )
        assert code is not None
        assert code.attributes["discovered_from_report"] is True


class _FakeDiscoveryConnector:
    """Duck-typed :class:`TurizmConnector` for the discovery command tests."""

    def __init__(self, plans, *, institution_code, fail_dataset=None) -> None:
        self.institution_code = institution_code
        self.institution_name = "Turizm discover test"
        self._plans = plans
        self._fail_dataset = fail_dataset
        self.built: list[str] = []

    def discovery_plans(self, *, page=None):
        return list(self._plans)

    def dataset_meta(self, dataset_code):
        return _report_meta(dataset_code)

    def build_report(self, plan):
        self.built.append(plan.dataset_code)
        if plan.dataset_code == self._fail_dataset:
            raise RuntimeError("boom")
        return [
            InterpretedSeries(
                codes={"VARIABLE": plan.variable, "KATEGORI": "Almanya"},
                points=[(date(2012, 1, 1), Decimal("1000"))],
                dataset=plan.dataset_code,
            )
        ]


def _discovery_plan(dataset_code: str) -> ReportPlan:
    path = FORM_PATH_BY_CODE["TUIK_TURIZM_CIKIS_GELIR_A"]
    return ReportPlan(dataset_code, path, None, "Milliyet", "2012", WalkResult())


def _observations_for(session, institution_code: str, dataset_code: str) -> int:
    institution = session.scalar(sa.select(Institution).where(Institution.code == institution_code))
    dataset = session.scalar(
        sa.select(Dataset).where(
            Dataset.institution_id == institution.id,
            Dataset.external_code == dataset_code,
        )
    )
    if dataset is None:
        return 0
    return session.scalar(
        sa.select(sa.func.count())
        .select_from(Observation)
        .join(Series, Observation.series_id == Series.id)
        .where(Series.dataset_id == dataset.id)
    )


def test_turizm_discover_commits_per_report_and_continues() -> None:
    institution_code = _unique("inst-turizm-disc")
    plans = [
        _discovery_plan(_unique("TUIK_TURIZM_DISC_A")),
        _discovery_plan(_unique("TUIK_TURIZM_DISC_B")),
    ]
    connector = _FakeDiscoveryConnector(
        plans, institution_code=institution_code, fail_dataset=plans[1].dataset_code
    )

    with SessionLocal() as session:
        assert _cmd_turizm_discover(session, connector, argparse.Namespace(page=None)) == 0

    with SessionLocal() as session:
        # The first report was committed before the second report failed.
        assert _observations_for(session, institution_code, plans[0].dataset_code) == 1
        assert _observations_for(session, institution_code, plans[1].dataset_code) == 0


def test_turizm_discover_skips_already_loaded_reports() -> None:
    institution_code = _unique("inst-turizm-resume")
    plans = [_discovery_plan(_unique("TUIK_TURIZM_RESUME"))]
    first = _FakeDiscoveryConnector(plans, institution_code=institution_code)
    with SessionLocal() as session:
        assert _cmd_turizm_discover(session, first, argparse.Namespace(page=None)) == 0
    assert first.built == [plans[0].dataset_code]

    second = _FakeDiscoveryConnector(plans, institution_code=institution_code)
    with SessionLocal() as session:
        assert _cmd_turizm_discover(session, second, argparse.Namespace(page=None)) == 0
    assert second.built == []


def test_turizm_external_code_resolves_in_dimension_order() -> None:
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "Turizm resolve")
        _, dataset = upsert_dataset(
            session, institution.id, _small_meta(_unique("TUIK_TURIZM_RES"))
        )
        session.commit()
        external_code = dataset.external_code
        codes = resolve_external_code(dataset, f"{external_code}:Almanya.Esenboğa")
    assert codes == {"MILLIYET": "Almanya", "KAPI": "Esenboğa"}
