"""Integration tests for the connector base flow (isolated `karven-test` stack).

They exercise ``sync_catalog`` + ``ensure_series`` + ``ingest_series`` through a
FAKE connector so no network is touched, but the database and MinIO are the
real test stack.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.connectors.base import (
    NOT_FOUND,
    ROLE_FREQUENCY,
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    SourceConnector,
    ensure_series,
    ingest_series,
    store_raw,
    sync_catalog,
    upsert_dataset,
    upsert_institution,
)
from app.connectors.tuik.__main__ import _cmd_catalog
from app.connectors.tuik.parsers import DataflowInfo
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset, DatasetDimension, DimensionCode, Institution, Series
from app.data.observations import get_latest
from app.db.session import SessionLocal
from app.migrator.minio import create_client

pytestmark = pytest.mark.integration

PERIODS = [(date(2024, 1, 1), Decimal("1.5")), (date(2024, 2, 1), None)]


def _dataset_meta(external_code: str, freq_codes: list[str]) -> DatasetMeta:
    return DatasetMeta(
        external_code=external_code,
        name=f"{external_code} dataset",
        source_category="Tests / Fake",
        coverage_start=date(2024, 1, 1),
        coverage_end=date(2024, 2, 1),
        obs_count=2,
        dimensions=[
            DimensionMeta(
                code="REF_AREA",
                label="Reference area",
                position=0,
                role=ROLE_GEO,
                codes=[DimensionCodeMeta(code="TR", label="Türkiye")],
            ),
            DimensionMeta(
                code="FREQ",
                label="Frequency",
                position=1,
                role=ROLE_FREQUENCY,
                codes=[DimensionCodeMeta(code=code, label=code) for code in freq_codes],
            ),
            DimensionMeta(
                code="TIME_PERIOD", label="Time period", position=2, role=ROLE_TIME, codes=[]
            ),
        ],
    )


class ListConnector(SourceConnector):
    """Deterministic in-memory connector writing real raw objects to MinIO."""

    institution_name = "Fake Source"
    channel = "fake"

    def __init__(
        self, institution_code: str, metas: list[DatasetMeta], *, with_points: bool = True
    ) -> None:
        self.institution_code = institution_code
        self._metas = metas
        self._with_points = with_points

    def list_datasets(self) -> Iterator[DatasetMeta]:
        yield from self._metas

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        points = [point for point in PERIODS if point[0] >= start] if self._with_points else []
        key = store_raw(
            self.institution_code, dataset_code, self.channel, b'{"fake": true}', "json"
        )
        external_code = f"{dataset_code}:" + ".".join(codes[dim] for dim in (order or codes))
        return FetchResult(
            external_code=external_code,
            points=points,
            raw_object_keys=[key],
            channel=self.channel,
        )


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def test_sync_catalog_and_ingest_series_roundtrip() -> None:
    institution_code = _unique("conn")
    connector = ListConnector(institution_code, [_dataset_meta("FAKE", ["M"])])

    with SessionLocal() as session:
        first = sync_catalog(session, connector)
        session.commit()

    with SessionLocal() as session:
        second = sync_catalog(session, connector)
        session.commit()

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.unchanged == 1

    with SessionLocal() as session:
        dataset = session.scalar(
            select(Dataset).where(
                Dataset.institution_id == first.institution_id,
                Dataset.external_code == "FAKE",
            )
        )
        assert dataset is not None
        assert dataset.obs_count == 2
        assert dataset.source_category == "Tests / Fake"
        # No series rows are created by a catalog sync.
        assert session.scalar(select(func.count()).select_from(Series)) == 0
        dataset_id = dataset.id

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        series = ensure_series(session, dataset, {"REF_AREA": "TR", "FREQ": "M"})
        session.commit()
        series_id = series.id
        assert series.dataset_id == dataset_id
        assert series.external_code == "FAKE:TR.M"
        assert series.frequency == "monthly"

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        result = ingest_series(
            session, connector, dataset=dataset, codes={"REF_AREA": "TR", "FREQ": "M"}
        )
        session.commit()

    assert result.inserted == 2
    assert result.unchanged == 0
    assert result.point_count == 2
    assert result.period_start == date(2024, 1, 1)
    assert result.period_end == date(2024, 2, 1)
    assert result.raw_object_key is not None
    assert result.raw_object_key.startswith(f"sources/{institution_code}/")

    with SessionLocal() as session:
        rows = get_latest(session, series_id)
    assert [row.period for row in rows] == [date(2024, 1, 1), date(2024, 2, 1)]
    assert rows[0].value == Decimal("1.5")
    assert rows[1].value is None
    assert rows[0].raw_object_key == result.raw_object_key
    assert rows[0].fetched_at.tzinfo is not None
    assert abs((datetime.now(UTC) - rows[0].fetched_at).total_seconds()) < 300

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        again = ingest_series(
            session, connector, dataset=dataset, codes={"REF_AREA": "TR", "FREQ": "M"}
        )
        session.commit()
    assert again.inserted == 0
    assert again.unchanged == 2

    with SessionLocal() as session:
        series = session.get(Series, series_id)
        assert series is not None
        assert series.coverage_start == date(2024, 1, 1)
        assert series.coverage_end == date(2024, 2, 1)

    _assert_raw_object_exists(result.raw_object_key)


def test_ensure_series_is_idempotent() -> None:
    institution_code = _unique("conn")
    connector = ListConnector(institution_code, [_dataset_meta("FAKE", ["M"])])
    with SessionLocal() as session:
        sync_catalog(session, connector)
        session.commit()

    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == institution_code)
        )
        assert institution is not None
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution.id))
        assert dataset is not None
        first = ensure_series(session, dataset, {"REF_AREA": "TR", "FREQ": "M"})
        session.commit()
        first_id = first.id
        institution_id = institution.id

    with SessionLocal() as session:
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution_id))
        assert dataset is not None
        second = ensure_series(session, dataset, {"REF_AREA": "TR", "FREQ": "M"})
        session.commit()
        assert second.id == first_id

    with SessionLocal() as session:
        count = session.scalar(
            select(func.count()).select_from(Series).where(Series.institution_id == institution_id)
        )
    assert count == 1


def test_sync_catalog_adds_new_dimension_and_moves_positions() -> None:
    institution_code = _unique("conn")
    base = _dataset_meta("FAKE", ["M"])
    expanded = replace(
        base,
        dimensions=[
            base.dimensions[0],
            DimensionMeta(
                code="SEX",
                label="Sex",
                position=1,
                role=ROLE_OTHER,
                codes=[DimensionCodeMeta(code="_T", label="Total")],
            ),
            replace(base.dimensions[1], position=2),
            replace(base.dimensions[2], position=3),
        ],
    )
    with SessionLocal() as session:
        sync_catalog(session, ListConnector(institution_code, [base]))
        session.commit()
    with SessionLocal() as session:
        outcome = sync_catalog(session, ListConnector(institution_code, [expanded]))
        session.commit()

    assert outcome.updated == 1
    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == institution_code)
        )
        assert institution is not None
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution.id))
        assert dataset is not None
        dims = {dimension.code: dimension for dimension in dataset.dimensions}
        assert dims["SEX"].position == 1
        assert dims["SEX"].codes[0].code == "_T"
        assert dims["FREQ"].position == 2
        assert dims["TIME_PERIOD"].position == 3


def test_sync_catalog_marks_removed_codes_and_keeps_them() -> None:
    institution_code = _unique("conn")
    with SessionLocal() as session:
        sync_catalog(session, ListConnector(institution_code, [_dataset_meta("FAKE", ["M", "A"])]))
        session.commit()

    with SessionLocal() as session:
        sync_catalog(session, ListConnector(institution_code, [_dataset_meta("FAKE", ["M"])]))
        session.commit()

    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == institution_code)
        )
        assert institution is not None
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution.id))
        assert dataset is not None
        freq = session.scalar(
            select(DatasetDimension).where(
                DatasetDimension.dataset_id == dataset.id,
                DatasetDimension.code == "FREQ",
            )
        )
        assert freq is not None
        codes = {
            code.code: code
            for code in session.scalars(
                select(DimensionCode).where(DimensionCode.dimension_id == freq.id)
            ).all()
        }
    assert set(codes) == {"M", "A"}
    assert codes["M"].attributes.get("removed_at") is None
    assert codes["A"].attributes.get("removed_at") is not None


def test_sync_catalog_widens_but_never_shrinks_coverage() -> None:
    institution_code = _unique("conn")
    wide = replace(
        _dataset_meta("FAKE", ["A"]), coverage_start=date(2020, 1, 1), coverage_end=date(2024, 1, 1)
    )
    narrow = replace(
        _dataset_meta("FAKE", ["A"]), coverage_start=date(2023, 1, 1), coverage_end=date(2023, 1, 1)
    )
    with SessionLocal() as session:
        sync_catalog(session, ListConnector(institution_code, [wide]))
        session.commit()
    with SessionLocal() as session:
        sync_catalog(session, ListConnector(institution_code, [narrow], with_points=False))
        session.commit()
        institution = session.scalar(
            select(Institution).where(Institution.code == institution_code)
        )
        assert institution is not None
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution.id))
        assert dataset is not None
        assert dataset.coverage_start == date(2020, 1, 1)
        assert dataset.coverage_end == date(2024, 1, 1)


def test_ensure_series_rejects_invalid_codes() -> None:
    institution_code = _unique("conn")
    connector = ListConnector(institution_code, [_dataset_meta("FAKE", ["M"])])
    with SessionLocal() as session:
        sync_catalog(session, connector)
        session.commit()
    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == institution_code)
        )
        assert institution is not None
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution.id))
        assert dataset is not None
        with pytest.raises(SeriesDefinitionError):
            ensure_series(session, dataset, {"REF_AREA": "TR", "FREQ": "Q"})
        session.rollback()


class _StubClient:
    max_concurrency = 2
    throttled = False


class MixedConnector:
    """Catalog-command stub: one good dataflow, one raising a raw ValueError."""

    institution_name = "Mixed Source"
    channel = "mixed"
    client = _StubClient()

    def __init__(self) -> None:
        self.institution_code = _unique("mixed")

    def dataflows(self) -> list[DataflowInfo]:
        return [
            DataflowInfo(
                dataflow_id=dataflow_id,
                version="1.0",
                agency="TR",
                title=dataflow_id,
                description=None,
                source_category=None,
            )
            for dataflow_id in ("DF_GOOD", "DF_BAD")
        ]

    def unlisted_dataflows(self) -> list[DataflowInfo]:
        return []

    def dataset_meta(self, info: DataflowInfo) -> DatasetMeta:
        if info.dataflow_id == "DF_BAD":
            raise ValueError("parser bug")
        return _dataset_meta("DF_GOOD", ["A"])


def test_catalog_command_continues_after_a_failing_dataflow() -> None:
    connector = MixedConnector()
    args = argparse.Namespace(dry_run=False, limit=None, dataflow=None, verify_completeness=False)

    with SessionLocal() as session:
        assert _cmd_catalog(session, connector, args) == 0

    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == connector.institution_code)
        )
        assert institution is not None
        dataset_count = session.scalar(
            select(func.count())
            .select_from(Dataset)
            .where(Dataset.institution_id == institution.id)
        )
    assert dataset_count == 1


def _assert_raw_object_exists(key: str) -> None:
    client = create_client(
        settings.minio_endpoint or "",
        settings.minio_access_key or "",
        settings.minio_secret_key.get_secret_value() if settings.minio_secret_key else "",
    )
    try:
        response = client.get_object(Bucket=settings.minio_bucket_raw, Key=key)
        assert response["Body"].read() == b'{"fake": true}'
    finally:
        client.close()


def test_new_dataset_is_usable_in_the_same_transaction() -> None:
    # Regression (found live 2026-09-30): right after upsert_dataset inserted a
    # dataset, ensure_series in the same session saw no dimensions, so the first
    # Turcat run skipped every indicator.
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("conn"), "Fake Source")
        _, dataset = upsert_dataset(session, institution.id, _dataset_meta("FAKE", ["M"]))
        series = ensure_series(session, dataset, {"REF_AREA": "TR", "FREQ": "M"})
        assert series.id is not None
        assert {d.code for d in dataset.dimensions} >= {"REF_AREA", "FREQ", "TIME_PERIOD"}
        session.rollback()


class UnlistedFlowConnector:
    """One dataflow whose listing state and source answer the test switches."""

    institution_name = "Unlisted Source"
    channel = "databrowser2"
    client = _StubClient()

    def __init__(self) -> None:
        self.institution_code = _unique("unl")
        self.listed = True
        self.answer: Exception | None = None

    def _info(self, *, listed: bool) -> DataflowInfo:
        return DataflowInfo(
            dataflow_id="DF_FLOW",
            version="1.0",
            agency="TR",
            title="DF_FLOW",
            description=None,
            source_category=None,
            listed=listed,
        )

    def dataflows(self) -> list[DataflowInfo]:
        return [self._info(listed=True)] if self.listed else []

    def unlisted_dataflows(self) -> list[DataflowInfo]:
        return [] if self.listed else [self._info(listed=False)]

    def dataset_meta(self, info: DataflowInfo) -> DatasetMeta:
        if self.answer is not None:
            raise self.answer
        return replace(
            _dataset_meta("DF_FLOW", ["A"]),
            attributes={"channel": "databrowser2", "dataflow_id": "DF_FLOW"},
        )


def test_catalog_command_writes_keeps_and_clears_unlisted_markers() -> None:
    connector = UnlistedFlowConnector()
    args = argparse.Namespace(dry_run=False, limit=None, dataflow=None, verify_completeness=False)

    def attributes_and_dims() -> tuple[dict, int]:
        with SessionLocal() as session:
            institution = session.scalar(
                select(Institution).where(Institution.code == connector.institution_code)
            )
            dataset = session.scalar(
                select(Dataset).where(Dataset.institution_id == institution.id)
            )
            return dict(dataset.attributes), len(dataset.dimensions)

    with SessionLocal() as session:
        assert _cmd_catalog(session, connector, args) == 0
    listed_attrs, dims = attributes_and_dims()
    assert "unlisted_since" not in listed_attrs

    # dropped from the listing but still answering: marker written
    connector.listed = False
    with SessionLocal() as session:
        assert _cmd_catalog(session, connector, args) == 0
    attrs, _ = attributes_and_dims()
    assert "removed_at" not in attrs

    # pretend it was first missed earlier: the earliest date is kept
    with SessionLocal() as session:
        institution = session.scalar(
            select(Institution).where(Institution.code == connector.institution_code)
        )
        dataset = session.scalar(select(Dataset).where(Dataset.institution_id == institution.id))
        dataset.attributes = {**dataset.attributes, "unlisted_since": "2020-01-01"}
        session.commit()
    with SessionLocal() as session:
        assert _cmd_catalog(session, connector, args) == 0
    attrs, _ = attributes_and_dims()
    assert attrs["unlisted_since"] == "2020-01-01"

    # source stops answering: removed_at set, dimensions and other attributes untouched
    connector.answer = ConnectorError(NOT_FOUND, "gone")
    with SessionLocal() as session:
        assert _cmd_catalog(session, connector, args) == 0
    attrs, removed_dims = attributes_and_dims()
    assert attrs["removed_at"] == date.today().isoformat()
    assert attrs["unlisted_since"] == "2020-01-01"
    assert attrs["channel"] == "databrowser2"
    assert removed_dims == dims

    # back in the listing: both markers cleared by the wholesale attribute replace
    connector.listed = True
    connector.answer = None
    with SessionLocal() as session:
        assert _cmd_catalog(session, connector, args) == 0
    attrs, _ = attributes_and_dims()
    assert "unlisted_since" not in attrs
    assert "removed_at" not in attrs
