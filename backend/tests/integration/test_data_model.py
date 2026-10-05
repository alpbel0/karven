"""Integration tests for the data layer (isolated `karven-test` stack)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import settings
from app.data import fetch_jobs, periods
from app.data.models import Dataset, FetchJob, Institution, Series
from app.data.observations import RecordResult, get_latest, record_observations
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _make_institution(session, code: str) -> Institution:
    institution = Institution(code=code, name=code)
    session.add(institution)
    session.flush()
    return institution


def _make_dataset(session, institution_id: int, external_code: str) -> Dataset:
    dataset = Dataset(
        institution_id=institution_id,
        external_code=external_code,
        name=external_code,
    )
    session.add(dataset)
    session.flush()
    return dataset


def _make_series(
    session, institution_id: int, external_code: str, frequency: str = periods.MONTHLY
) -> Series:
    dataset = _make_dataset(session, institution_id, f"ds-{external_code}")
    series = Series(
        institution_id=institution_id,
        dataset_id=dataset.id,
        external_code=external_code,
        name=external_code,
        frequency=frequency,
    )
    session.add(series)
    session.flush()
    return series


def test_observation_without_fetched_at_is_rejected() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        series = _make_series(session, institution.id, _unique("ext"))
        session.commit()
        series_id = series.id

    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(
                    text(
                        "INSERT INTO observations (series_id, period, value) "
                        "VALUES (:series_id, DATE '2024-09-01', 1.5)"
                    ),
                    {"series_id": series_id},
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_update_and_delete_are_rejected_by_trigger() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        series = _make_series(session, institution.id, _unique("ext"))
        record_observations(
            session,
            series.id,
            [(date(2024, 9, 1), Decimal("1.5"))],
            fetched_at=datetime(2024, 10, 1, tzinfo=UTC),
        )
        session.commit()
        series_id = series.id

    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            observation_id = connection.execute(
                text("SELECT id FROM observations WHERE series_id = :sid"),
                {"sid": series_id},
            ).scalar_one()

            with pytest.raises(DBAPIError):
                connection.execute(
                    text("UPDATE observations SET value = 9 WHERE id = :id"),
                    {"id": observation_id},
                )
            connection.rollback()

            with pytest.raises(DBAPIError):
                connection.execute(
                    text("DELETE FROM observations WHERE id = :id"),
                    {"id": observation_id},
                )
            connection.rollback()

            remaining = connection.execute(
                text("SELECT count(*) FROM observations WHERE id = :id"),
                {"id": observation_id},
            ).scalar_one()
            assert remaining == 1
    finally:
        engine.dispose()


def test_revision_flow_keeps_history_and_latest() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        series = _make_series(session, institution.id, _unique("ext"))
        session.commit()
        series_id = series.id

    period = date(2024, 9, 1)
    with SessionLocal() as session:
        first = record_observations(
            session,
            series_id,
            [(period, Decimal("1.50"))],
            fetched_at=datetime(2024, 10, 1, tzinfo=UTC),
            raw_object_key="raw/1",
        )
        session.commit()
        assert first == RecordResult(inserted=1, unchanged=0)

        same = record_observations(
            session,
            series_id,
            [(period, Decimal("1.5"))],
            fetched_at=datetime(2024, 10, 2, tzinfo=UTC),
        )
        session.commit()
        assert same == RecordResult(inserted=0, unchanged=1)

        changed = record_observations(
            session,
            series_id,
            [(period, Decimal("2.0"))],
            fetched_at=datetime(2024, 10, 3, tzinfo=UTC),
            raw_object_key="raw/3",
        )
        session.commit()
        assert changed == RecordResult(inserted=1, unchanged=0)

    with SessionLocal() as session:
        latest = get_latest(session, series_id)
        assert len(latest) == 1
        assert latest[0].period == period
        assert latest[0].value == Decimal("2.0")
        assert latest[0].raw_object_key == "raw/3"

    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            history = connection.execute(
                text("SELECT count(*) FROM observations WHERE series_id = :sid"),
                {"sid": series_id},
            ).scalar_one()
        assert history == 2
    finally:
        engine.dispose()


def test_null_value_then_number_counts_as_revision() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        series = _make_series(session, institution.id, _unique("ext"))
        session.commit()
        series_id = series.id

    period = date(2024, 8, 1)
    with SessionLocal() as session:
        inserted = record_observations(
            session,
            series_id,
            [(period, None)],
            fetched_at=datetime(2024, 10, 4, tzinfo=UTC),
        )
        session.commit()
        assert inserted == RecordResult(inserted=1, unchanged=0)

        unchanged = record_observations(
            session,
            series_id,
            [(period, None)],
            fetched_at=datetime(2024, 10, 5, tzinfo=UTC),
        )
        session.commit()
        assert unchanged == RecordResult(inserted=0, unchanged=1)

        revised = record_observations(
            session,
            series_id,
            [(period, Decimal("7"))],
            fetched_at=datetime(2024, 10, 6, tzinfo=UTC),
        )
        session.commit()
        assert revised == RecordResult(inserted=1, unchanged=0)

    engine = create_engine(settings.postgres_url)
    try:
        with engine.connect() as connection:
            null_rows = connection.execute(
                text("SELECT count(*) FROM observations WHERE series_id = :sid AND value IS NULL"),
                {"sid": series_id},
            ).scalar_one()
            assert null_rows == 1
    finally:
        engine.dispose()

    with SessionLocal() as session:
        latest = {row.period: row.value for row in get_latest(session, series_id)}
        assert latest[period] == Decimal("7")


def test_series_requires_a_dataset() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        session.flush()
        series = Series(
            institution_id=institution.id,
            external_code=_unique("ext"),
            name="no dataset",
            frequency=periods.MONTHLY,
        )
        session.add(series)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_series_unique_and_frequency_check() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        external_code = _unique("ext")
        _make_series(session, institution.id, external_code)
        session.commit()
        institution_id = institution.id

    with SessionLocal() as session:
        with pytest.raises(IntegrityError):
            _make_series(session, institution_id, external_code)
        session.rollback()

    with SessionLocal() as session:
        with pytest.raises(IntegrityError):
            _make_series(session, institution_id, _unique("ext"), frequency="hourly")
        session.rollback()


def test_only_one_active_fetch_job_per_series() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        session.commit()
        institution_id = institution.id

    external_code = _unique("ext")
    with SessionLocal() as session:
        first = fetch_jobs.create_job(
            session, institution_id=institution_id, external_code=external_code
        )
        session.commit()
        first_id = first.id

    with SessionLocal() as session:
        with pytest.raises(IntegrityError):
            fetch_jobs.create_job(
                session, institution_id=institution_id, external_code=external_code
            )
        session.rollback()

    with SessionLocal() as session:
        job = session.get(FetchJob, first_id)
        assert job is not None
        fetch_jobs.mark_status(session, job, fetch_jobs.COMPLETED)
        session.commit()

    with SessionLocal() as session:
        second = fetch_jobs.create_job(
            session, institution_id=institution_id, external_code=external_code
        )
        session.commit()
        assert second.id != first_id


def test_biennial_series_is_accepted_and_period_alignment_is_enforced() -> None:
    """Migration 0020 widened the frequency check; biennial uses January 1 periods.

    Nothing is committed: observations are immutable, and a committed ``biennial``
    series would (correctly) make the 0020 downgrade refuse in the migration
    round-trip tests that share this database.
    """
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        series = _make_series(session, institution.id, _unique("ext"), frequency=periods.BIENNIAL)
        result = record_observations(
            session,
            series.id,
            [(date(2020, 1, 1), Decimal("1.5")), (date(2022, 1, 1), Decimal("2.5"))],
            fetched_at=datetime.now(UTC),
        )
        assert result.inserted == 2
        assert series.frequency == "biennial"

        with pytest.raises(periods.PeriodError):
            record_observations(
                session,
                series.id,
                [(date(2024, 7, 1), Decimal("3.5"))],
                fetched_at=datetime.now(UTC),
            )
        session.rollback()


def test_series_frequency_check_still_rejects_unknown_values() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session, _unique("inst"))
        dataset = _make_dataset(session, institution.id, _unique("ds"))
        session.add(
            Series(
                institution_id=institution.id,
                dataset_id=dataset.id,
                external_code=_unique("ext"),
                name="bogus",
                frequency="fortnightly",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
