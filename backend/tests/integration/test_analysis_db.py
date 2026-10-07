"""Integration test: the relation test engine against a real database (Task 3.2).

Written for the isolated ``karven-test`` stack; not run by the unit suite. Run with
``uv run python scripts/integration.py``. Observations are immutable (a trigger rejects
delete), so nothing is committed: every test flushes inside one session and rolls back.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest

from app.analysis.loader import load_series
from app.analysis.models import MissingData, RelationSpec, TestReport, Unsupported
from app.analysis.runner import run_relation, to_graph_period_results
from app.analysis.significance import HacOls
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


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _dataset(session, institution: Institution, *, frequency: str, aggregation: str) -> Dataset:
    dataset = Dataset(
        institution_id=institution.id,
        external_code=_unique("DS"),
        name="Analiz Testi",
        attributes={},
        coverage_start=date(2000, 1, 1),
        coverage_end=date(2026, 8, 1),
    )
    session.add(dataset)
    session.flush()
    dimension = DatasetDimension(
        dataset_id=dataset.id, code="INDICATOR", label="Gösterge", position=0, role="other"
    )
    session.add(dimension)
    session.flush()
    for code in ("TGT", "DRV"):
        session.add(
            DimensionCode(
                dimension_id=dimension.id,
                code=code,
                label=code,
                attributes={"unit": "Endeks", "frequency": frequency},
            )
        )
        session.add(
            MeasureCombination(
                dataset_id=dataset.id,
                codes={"INDICATOR": code},
                label=code,
                measure_type="endeks",
                data_nature="gerceklesen",
                aggregation=aggregation,
            )
        )
    session.flush()
    return dataset


def _series(session, institution: Institution, dataset: Dataset, code: str) -> Series:
    definition = build_series_definition(dataset, {"INDICATOR": code})
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


def _fill(session, series: Series, points: dict[date, float]) -> None:
    fetched_at = datetime.now(UTC)
    for period, value in points.items():
        session.add(
            Observation(
                series_id=series.id,
                period=period,
                value=Decimal(str(round(value, 6))),
                fetched_at=fetched_at,
            )
        )
    session.flush()


def _month(index: int) -> date:
    year, month = divmod(2005 * 12 + index, 12)
    return date(year, month + 1, 1)


def _linked_monthly(n: int = 252, seed: int = 1) -> tuple[dict[date, float], dict[date, float]]:
    rng = np.random.default_rng(seed)
    driver_inc = rng.normal(size=n)
    target_inc = rng.normal(scale=0.5, size=n)
    target_inc[1:] += 0.8 * driver_inc[:-1]
    driver = 100.0 + np.cumsum(driver_inc)
    target = 100.0 + np.cumsum(target_inc)
    return (
        {_month(i): float(v) for i, v in enumerate(target)},
        {_month(i): float(v) for i, v in enumerate(driver)},
    )


def _key(institution: Institution, series: Series) -> str:
    return f"{institution.code}|{series.external_code}"


def test_a_loaded_relation_is_tested_end_to_end_and_converts_to_graph_results() -> None:
    target_points, driver_points = _linked_monthly()
    with SessionLocal() as session:
        institution = Institution(code=_unique("an"), name="Analysis Test")
        session.add(institution)
        session.flush()
        dataset = _dataset(session, institution, frequency="monthly", aggregation="ortalama")
        target = _series(session, institution, dataset, "TGT")
        driver = _series(session, institution, dataset, "DRV")
        _fill(session, target, target_points)
        _fill(session, driver, driver_points)
        spec = RelationSpec(
            _key(institution, target),
            {_key(institution, driver): "positive"},
            0,
            3,
            "difference",
        )
        report = run_relation(session, spec, method=HacOls(calibrated=True))
        loaded = load_series(session, _key(institution, target))
        session.rollback()
    assert isinstance(report, TestReport)
    assert report.status == "supported"
    assert report.windows["all_years"].best_lag == 1
    assert loaded is not None and loaded.aggregation == "ortalama" and loaded.step == 1
    results = to_graph_period_results(report)
    assert [item.period for item in results] == ["all_years", "2017_2026"]
    assert all(item.reliable for item in results)


def test_unloaded_series_are_reported_as_missing_not_tested() -> None:
    with SessionLocal() as session:
        institution = Institution(code=_unique("an"), name="Analysis Test")
        session.add(institution)
        session.flush()
        dataset = _dataset(session, institution, frequency="monthly", aggregation="ortalama")
        target = _series(session, institution, dataset, "TGT")  # row exists, no observations
        _fill(session, target, {_month(0): 1.0})
        missing_key = f"{institution.code}|never-created"
        spec = RelationSpec(
            _key(institution, target), {missing_key: "positive"}, 0, 2, "difference"
        )
        outcome = run_relation(session, spec)
        empty = _series(session, institution, dataset, "DRV")  # a row without any observation
        spec_empty = RelationSpec(
            _key(institution, target), {_key(institution, empty): "positive"}, 0, 2, "difference"
        )
        outcome_empty = run_relation(session, spec_empty)
        session.rollback()
    assert isinstance(outcome, MissingData)
    assert outcome.series == (missing_key,)
    assert isinstance(outcome_empty, MissingData)
    assert outcome_empty.series == (_key(institution, empty),)


def test_a_daily_series_is_reduced_to_closed_months_without_holes() -> None:
    with SessionLocal() as session:
        institution = Institution(code=_unique("an"), name="Analysis Test")
        session.add(institution)
        session.flush()
        dataset = _dataset(session, institution, frequency="daily", aggregation="ortalama")
        series = _series(session, institution, dataset, "TGT")
        points: dict[date, float] = {}
        day = date(2024, 1, 1)
        while day <= date(2024, 4, 10):
            if day.weekday() < 5 and not (date(2024, 2, 10) <= day <= date(2024, 2, 28)):
                points[day] = 10.0 + day.month  # February has a 19-day hole
            day += timedelta(days=1)
        _fill(session, series, points)
        loaded = load_series(session, _key(institution, series))
        session.rollback()
    assert loaded is not None
    assert loaded.frequency == "monthly" and loaded.step == 1
    months = {(date.year, date.month) for date in (_month_of(slot) for slot in loaded.values.index)}
    # January (closed, no hole) and March (closed) stay; February has a hole; April is still open
    assert months == {(2024, 1), (2024, 3)}
    assert list(loaded.values) == [11.0, 13.0]


def _month_of(slot: int) -> date:
    return date(slot // 12, slot % 12 + 1, 1)


def test_a_weight_measure_is_unsupported_through_the_loader_path() -> None:
    target_points, driver_points = _linked_monthly(n=60)
    with SessionLocal() as session:
        institution = Institution(code=_unique("an"), name="Analysis Test")
        session.add(institution)
        session.flush()
        dataset = _dataset(session, institution, frequency="monthly", aggregation="test_disi")
        target = _series(session, institution, dataset, "TGT")
        driver = _series(session, institution, dataset, "DRV")
        _fill(session, target, target_points)
        _fill(session, driver, driver_points)
        spec = RelationSpec(
            _key(institution, target), {_key(institution, driver): "positive"}, 0, 2, "difference"
        )
        outcome = run_relation(session, spec)
        session.rollback()
    assert isinstance(outcome, Unsupported)
