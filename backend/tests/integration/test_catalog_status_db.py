"""Integration test: ``data_status`` against a real session (Task 2.5).

Written for the isolated ``karven-test`` stack; intentionally NOT run by the unit
suite. Run with ``uv run python scripts/integration.py``.

Observations are immutable (a trigger rejects delete), so nothing is committed:
every test flushes inside one session and rolls back.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.catalog.status import data_status
from app.connectors.base import build_series_definition
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    Institution,
    MeasureCombination,
    Observation,
    Series,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration

_SECRET_VALUES = (Decimal("918273.4455"), Decimal("123456.7891"))


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _institution(session) -> Institution:
    institution = Institution(code=_unique("st"), name="Status Test")
    session.add(institution)
    session.flush()
    return institution


def _dataset(session, institution: Institution, **kwargs) -> Dataset:
    dataset = Dataset(
        institution_id=institution.id,
        external_code=_unique("DS"),
        name="Durum Testi",
        attributes={},
        coverage_start=date(2010, 1, 1),
        coverage_end=date(2025, 6, 1),
        **kwargs,
    )
    session.add(dataset)
    session.flush()
    dimension = DatasetDimension(
        dataset_id=dataset.id, code="INDICATOR", label="Gösterge", position=0, role="other"
    )
    session.add(dimension)
    session.flush()
    session.add(
        DimensionCode(
            dimension_id=dimension.id,
            code="POP",
            label="Nüfus",
            attributes={"unit": "Kişi", "frequency": "monthly"},
        )
    )
    session.add(
        MeasureCombination(
            dataset_id=dataset.id,
            codes={"INDICATOR": "POP"},
            label="Nüfus",
            measure_type="stok_adet",
            data_nature="gerceklesen",
        )
    )
    session.flush()
    return dataset


def _recipe(institution: Institution, dataset: Dataset) -> dict:
    return {
        "institution": institution.code,
        "dataset": dataset.external_code,
        "codes": {"INDICATOR": "POP"},
    }


def _add_series(session, institution: Institution, dataset: Dataset) -> Series:
    definition = build_series_definition(dataset, {"INDICATOR": "POP"})
    series = Series(
        institution_id=institution.id,
        dataset_id=dataset.id,
        external_code=definition.external_code,
        name=definition.name,
        frequency=definition.frequency,
        unit=definition.unit,
        breakdown=definition.breakdown,
        dimension_codes=definition.dimension_codes,
        attributes=definition.attributes,
    )
    session.add(series)
    session.flush()
    return series


def test_series_not_yet_created_reports_source_range_and_not_loaded() -> None:
    with SessionLocal() as session:
        institution = _institution(session)
        dataset = _dataset(session, institution)
        result = data_status(session, [_recipe(institution, dataset)])
        session.rollback()
    row = result["results"][0]
    assert row["status"] == "ok"
    assert row["series_exists"] is False
    assert row["series_id"] is None
    assert row["loaded"] is False
    assert row["loaded_range"] is None
    assert row["available_range"] == {"start": "2010-01-01", "end": "2025-06-01"}
    assert row["frequency"] == "monthly"
    assert row["unit"] == "Kişi"
    assert row["transforms"] == ["level", "percent_change_period", "percent_change_annual"]


def test_loaded_series_reports_loaded_range_and_never_a_value() -> None:
    with SessionLocal() as session:
        institution = _institution(session)
        dataset = _dataset(session, institution)
        series = _add_series(session, institution, dataset)
        fetched_at = datetime.now(UTC)
        for period, value in (
            (date(2018, 1, 1), _SECRET_VALUES[0]),
            (date(2018, 2, 1), _SECRET_VALUES[1]),
            (date(2018, 2, 1), _SECRET_VALUES[0]),  # a re-fetch of the same period
        ):
            session.add(
                Observation(series_id=series.id, period=period, value=value, fetched_at=fetched_at)
            )
        session.flush()
        result = data_status(session, [_recipe(institution, dataset)])
        session.rollback()
    row = result["results"][0]
    assert row["series_exists"] is True
    assert row["loaded"] is True
    assert row["loaded_range"] == {"start": "2018-01-01", "end": "2018-02-01", "periods": 2}
    # The source range still comes from the dataset, not from what is loaded.
    assert row["available_range"] == {"start": "2010-01-01", "end": "2025-06-01"}
    text = json.dumps(result)
    for secret in _SECRET_VALUES:
        assert str(secret) not in text
        assert str(secret.normalize()) not in text
    assert '"value"' not in text


def test_series_row_without_observations_is_not_loaded() -> None:
    with SessionLocal() as session:
        institution = _institution(session)
        dataset = _dataset(session, institution)
        _add_series(session, institution, dataset)
        result = data_status(session, [_recipe(institution, dataset)])
        session.rollback()
    row = result["results"][0]
    assert row["series_exists"] is True
    assert row["loaded"] is False
    assert row["loaded_range"] is None


def test_unknown_dataset_veri_yok_and_bad_codes() -> None:
    with SessionLocal() as session:
        institution = _institution(session)
        no_data = _dataset(session, institution, veri_yok=True)
        good = _dataset(session, institution)
        result = data_status(
            session,
            [
                {"institution": institution.code, "dataset": "NOPE", "codes": {}},
                _recipe(institution, no_data),
                {**_recipe(institution, good), "codes": {"INDICATOR": "WRONG"}},
            ],
        )
        session.rollback()
    assert [row["status"] for row in result["results"]] == [
        "unknown_dataset",
        "veri_yok",
        "invalid_codes",
    ]
    assert result["results"][1]["loaded"] is False


def test_dataset_without_measure_gives_null_transforms() -> None:
    with SessionLocal() as session:
        institution = _institution(session)
        dataset = _dataset(session, institution)
        session.execute(
            sa.delete(MeasureCombination).where(MeasureCombination.dataset_id == dataset.id)
        )
        session.expire_all()
        result = data_status(session, [_recipe(institution, dataset)])
        session.rollback()
    row = result["results"][0]
    assert row["measure"] is None
    assert row["transforms"] is None


def test_several_recipes_keep_input_order() -> None:
    with SessionLocal() as session:
        institution = _institution(session)
        first = _dataset(session, institution)
        second = _dataset(session, institution)
        series = _add_series(session, institution, second)
        session.add(
            Observation(
                series_id=series.id,
                period=date(2020, 1, 1),
                value=_SECRET_VALUES[0],
                fetched_at=datetime.now(UTC),
            )
        )
        session.flush()
        result = data_status(session, [_recipe(institution, first), _recipe(institution, second)])
        session.rollback()
    assert [row["dataset"] for row in result["results"]] == [
        first.external_code,
        second.external_code,
    ]
    assert [row["loaded"] for row in result["results"]] == [False, True]
