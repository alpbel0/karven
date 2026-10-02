"""Integration tests for the bi.tuik Qlik connector (isolated ``karven-test``)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.connectors.base import (
    ROLE_OTHER,
    ROLE_TIME,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    ensure_series,
    upsert_dataset,
    upsert_institution,
)
from app.connectors.tuik.siniflama import (
    ClassificationData,
    ClassificationItemRow,
    DimensionLinkRow,
    SiniflamaVersion,
    link_dimensions,
    load_classifications,
)
from app.data.models import (
    Classification,
    Dataset,
    DatasetDimension,
    DimensionClassificationLink,
    DimensionCode,
    Institution,
)
from app.data.observations import get_latest, record_observations
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _small_meta(external_code: str) -> DatasetMeta:
    """A tiny bi_qlik dataset with one month of USD exports of chapter 94."""
    return DatasetMeta(
        external_code=external_code,
        name="TÜİK BI test dataset",
        attributes={
            "channel": "bi_qlik",
            "system": "gts",
            "app_id": "test-app",
            "default_frequency": "monthly",
        },
        dimensions=[
            DimensionMeta(
                code="FLOW",
                label="Akış",
                position=0,
                role=ROLE_OTHER,
                codes=[
                    DimensionCodeMeta(code="_T", label="Toplam", is_default=True),
                    DimensionCodeMeta(code="X", label="İhracat"),
                    DimensionCodeMeta(code="M", label="İthalat"),
                ],
            ),
            DimensionMeta(
                code="PRODUCT_HS",
                label="Ürün (GTİP)",
                position=1,
                role=ROLE_OTHER,
                codes=[
                    DimensionCodeMeta(code="_T", label="Toplam", is_default=True),
                    DimensionCodeMeta(
                        code="94",
                        label="Mobilyalar",
                        attributes={"level": 1, "field": "FASIL"},
                    ),
                ],
            ),
            DimensionMeta(
                code="MEASURE",
                label="Ölçü",
                position=2,
                role=ROLE_OTHER,
                codes=[
                    DimensionCodeMeta(
                        code="USD",
                        label="ABD Doları (USD)",
                        attributes={"measure": "DOLAR", "unit": "US Dollar"},
                    )
                ],
            ),
            DimensionMeta(code="TIME_PERIOD", label="Dönem", position=3, role=ROLE_TIME, codes=[]),
        ],
    )


def test_bi_catalog_upsert_writes_dimensions_and_codes() -> None:
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "BI test")
        meta = _small_meta(_unique("TUIK_BI_TEST"))
        outcome, dataset = upsert_dataset(session, institution.id, meta)
        session.commit()
        assert outcome == "inserted"
        dataset_id = dataset.id
        external_code = dataset.external_code

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        assert dataset.attributes["channel"] == "bi_qlik"
        dimensions = {
            dimension.code: dimension
            for dimension in session.scalars(
                sa.select(DatasetDimension).where(DatasetDimension.dataset_id == dataset_id)
            )
        }
        assert set(dimensions) == {"FLOW", "PRODUCT_HS", "MEASURE", "TIME_PERIOD"}
        flow_codes = session.scalars(
            sa.select(DimensionCode.code).where(DimensionCode.dimension_id == dimensions["FLOW"].id)
        ).all()
        assert set(flow_codes) == {"_T", "X", "M"}
        assert dataset.external_code == external_code


def test_bi_headline_series_is_recorded_and_read_back() -> None:
    external_code = _unique("TUIK_BI_HEAD")
    with SessionLocal() as session:
        institution = upsert_institution(session, _unique("inst"), "BI headline")
        _, dataset = upsert_dataset(session, institution.id, _small_meta(external_code))
        session.commit()
        dataset_id = dataset.id

    with SessionLocal() as session:
        dataset = session.get(Dataset, dataset_id)
        assert dataset is not None
        codes = {"FLOW": "X", "PRODUCT_HS": "94", "MEASURE": "USD"}
        series = ensure_series(session, dataset, codes)
        points = [
            (date(2026, 6, 1), Decimal("10")),
            (date(2026, 7, 1), Decimal("25620124967")),
        ]
        record_observations(
            session, series.id, points, fetched_at=datetime.now(UTC), raw_object_key="sources/x"
        )
        session.commit()
        series_id = series.id
        assert series.frequency == "monthly"
        assert series.unit == "US Dollar"
        assert series.external_code == f"{external_code}:X.94.USD"

    with SessionLocal() as session:
        latest = get_latest(session, series_id)
        assert [row.period for row in latest] == [date(2026, 6, 1), date(2026, 7, 1)]
        assert latest[-1].value == Decimal("25620124967")


def test_link_dimensions_picks_up_a_bi_qlik_dataset() -> None:
    version = SiniflamaVersion(
        external_id=_unique("bi-vid"), type_code="99", name="GTİP test", short_name="GTİP 2026"
    )
    items = [
        ClassificationItemRow("94", None, 1, "Mobilyalar", None),
        ClassificationItemRow("9403", None, 2, "Diğer mobilyalar", None),
        ClassificationItemRow("940330", None, 3, "Ahşap ofis mobilyaları", None),
    ]
    with SessionLocal() as session:
        load_classifications(session, [ClassificationData(version=version, items=items)])
        institution = Institution(code=_unique("inst"), name="BI link test")
        session.add(institution)
        session.flush()
        dataset = Dataset(
            institution_id=institution.id,
            external_code=_unique("TUIK_BI_LINK"),
            name="BI link dataset",
            attributes={"channel": "bi_qlik"},
        )
        session.add(dataset)
        session.flush()
        dimension = DatasetDimension(
            dataset_id=dataset.id, code="PRODUCT_HS", label="Ürün (GTİP)", position=0, role="other"
        )
        session.add(dimension)
        session.flush()
        for code, label in (
            ("94", "Mobilyalar"),
            ("9403", "Diğer mobilyalar"),
            ("940330", "Ahşap ofis mobilyaları"),
            ("_T", "Toplam"),
        ):
            session.add(DimensionCode(dimension_id=dimension.id, code=code, label=label))
        session.commit()
        dimension_id = dimension.id
        dataset_code = dataset.external_code
        classification_id = session.scalar(
            sa.select(Classification.id).where(Classification.external_id == version.external_id)
        )

    def load_link() -> DimensionClassificationLink | None:
        with SessionLocal() as session:
            return session.scalar(
                sa.select(DimensionClassificationLink).where(
                    DimensionClassificationLink.dimension_id == dimension_id,
                    DimensionClassificationLink.classification_id == classification_id,
                )
            )

    with SessionLocal() as session:
        result = link_dimensions(session, collect_report=True)
        session.commit()

    link = load_link()
    assert link is not None
    assert link.total_codes == 3
    assert link.matched_codes == 3
    assert link.coverage == 1.0
    assert link.label_agreement == 1.0
    assert link.attributes["rule"] == "union"
    assert link.attributes["group"] == "GTİP"
    assert link.attributes["union_coverage"] == 1.0
    assert link.attributes["version_coverage"] == 1.0

    assert any(
        row.qualified and row.dataset == dataset_code and row.dimension == "PRODUCT_HS"
        for row in result.report
    )
    assert isinstance(result.report[0], DimensionLinkRow)

    # Re-runnable: the link is unchanged, not duplicated.
    with SessionLocal() as session:
        rerun = link_dimensions(session)
        session.commit()
    assert rerun.unchanged >= 1
    assert load_link() is not None
