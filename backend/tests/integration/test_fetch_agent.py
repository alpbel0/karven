"""Integration tests for the data-fetch agent against the isolated test stack.

Real PostgreSQL paths are exercised (the failure row, the retry job and its
attributes, waiter re-attachment and the ``(job, round)`` unique constraint); the
chat client is a scripted fake, so no network and no LLM are touched. Rows in
``fetch_jobs``/``fetch_failures`` cannot be deleted from the shared test
database, so every test uses a unique institution/dataset code and asserts only
its own rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.data import fetch_jobs
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    FetchFailure,
    FetchJob,
    FetchJobWaiter,
    Institution,
    Series,
)
from app.data.observations import record_observations
from app.db.session import SessionLocal, create_db_engine
from app.fetching.agent import service
from tests.fetch_agent_fakes import ScriptedChatClient
from tests.llm_helpers import tool_call

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _catalog(session, *, institution_code: str, dataset_code: str, codes: tuple[str, ...]):
    # Find-or-create: one test catalogs several datasets under the same unique
    # institution, and ``institutions.code`` is globally unique.
    institution = session.scalar(sa.select(Institution).where(Institution.code == institution_code))
    if institution is None:
        institution = Institution(code=institution_code, name=institution_code)
        session.add(institution)
        session.flush()
    dataset = Dataset(
        institution_id=institution.id,
        external_code=dataset_code,
        name=dataset_code,
        attributes={"channel": "evds3", "default_frequency": "monthly"},
    )
    session.add(dataset)
    session.flush()
    dimension = DatasetDimension(
        dataset_id=dataset.id, code="SERIE", label="SERIE", position=0, role="other"
    )
    session.add(dimension)
    session.flush()
    for code in codes:
        session.add(DimensionCode(dimension_id=dimension.id, code=code, label=code))
    session.flush()
    return institution.id


def _make_failed_job(session, *, institution_id: int, dataset_code: str, code: str) -> int:
    job = fetch_jobs.create_job(
        session,
        institution_id=institution_id,
        external_code=f"{dataset_code}:{code}",
        attributes={
            "origin": "on_demand",
            "dataset_code": dataset_code,
            "codes": {"SERIE": code},
            "start": date(2000, 1, 1).isoformat(),
        },
    )
    fetch_jobs.attach_waiter(session, job, waiter_type="idea", waiter_id=_unique("w"))
    fetch_jobs.mark_failed(session, job, error_reason="not_found: no such series")
    session.commit()
    return job.id


def _final(category: str, *, retry=None) -> dict:
    return {
        "category": category,
        "diagnosis": "tahmin",
        "suggestion": "öneri",
        "retry": retry,
        "alternatives": [],
    }


def test_diagnose_records_a_retry_with_parent_attributes_and_waiters() -> None:
    institution_code = _unique("inst")
    failed_dataset = _unique("ds-failed")
    retry_dataset = _unique("ds-retry")
    with SessionLocal() as session:
        institution_id = _catalog(
            session,
            institution_code=institution_code,
            dataset_code=failed_dataset,
            codes=("Y",),
        )
        _catalog(
            session,
            institution_code=institution_code,
            dataset_code=retry_dataset,
            codes=("X",),
        )
        session.commit()
        job_id = _make_failed_job(
            session, institution_id=institution_id, dataset_code=failed_dataset, code="Y"
        )

    final = _final(
        "bad_request",
        retry={
            "institution": institution_code,
            "dataset": retry_dataset,
            "codes": {"SERIE": "X"},
        },
    )
    client = ScriptedChatClient(final)
    dispatched: list[int] = []

    result = service.diagnose_job(
        SessionLocal,
        job_id,
        chat_client_factory=lambda: client,
        max_rounds=2,
        dispatch=dispatched.append,
    )

    assert result.outcome == "retry_requested"
    assert client.closed is True

    # Second writer: read everything back on a fresh session.
    with SessionLocal() as session:
        failures = session.scalars(
            sa.select(FetchFailure).where(FetchFailure.fetch_job_id == job_id)
        ).all()
        assert len(failures) == 1
        failure = failures[0]
        assert failure.outcome == "retry_requested"
        assert failure.category == "bad_request"
        assert failure.retry_job_id == result.retry_job_id
        assert failure.prompt_key == "fetch_agent.system"
        assert failure.prompt_version is not None
        assert failure.llm_provider == "fake"

        retry_job = session.get(FetchJob, result.retry_job_id)
        assert retry_job is not None
        assert retry_job.attributes["origin"] == "agent_retry"
        assert retry_job.attributes["agent_round"] == 1
        assert retry_job.attributes["parent_job_id"] == job_id
        assert "last_enqueued_at" in retry_job.attributes
        assert dispatched == [retry_job.id]

        retry_waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == retry_job.id)
        ).all()
        assert [row.status for row in retry_waiters] == [fetch_jobs.WAITING]

        old_waiters = session.scalars(
            sa.select(FetchJobWaiter).where(FetchJobWaiter.fetch_job_id == job_id)
        ).all()
        assert [row.status for row in old_waiters] == [fetch_jobs.DROPPED]


def test_second_diagnose_is_a_no_op_and_the_unique_constraint_holds() -> None:
    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    with SessionLocal() as session:
        institution_id = _catalog(
            session, institution_code=institution_code, dataset_code=dataset_code, codes=("Y",)
        )
        session.commit()
        job_id = _make_failed_job(
            session, institution_id=institution_id, dataset_code=dataset_code, code="Y"
        )

    final = _final("not_in_source")
    first = service.diagnose_job(
        SessionLocal,
        job_id,
        chat_client_factory=lambda: ScriptedChatClient(final),
        max_rounds=2,
        dispatch=lambda _job: None,
    )
    assert first.outcome == "recorded"

    second_client = ScriptedChatClient(final)
    second = service.diagnose_job(
        SessionLocal,
        job_id,
        chat_client_factory=lambda: second_client,
        max_rounds=2,
        dispatch=lambda _job: None,
    )
    assert second.outcome == "skipped"
    assert second_client.calls == []

    with SessionLocal() as session:
        count = session.scalar(
            sa.select(sa.func.count())
            .select_from(FetchFailure)
            .where(FetchFailure.fetch_job_id == job_id)
        )
        assert count == 1

        # The unique (fetch_job_id, round) is the last line of defence.
        duplicate = FetchFailure(
            fetch_job_id=job_id,
            round=first_failure_round(session, job_id),
            institution_id=institution_id,
            external_code=f"{dataset_code}:Y",
            category="unknown",
            outcome="recorded",
            error_reason="duplicate",
            codes={},
            alternatives=[],
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def first_failure_round(session, job_id: int) -> int:
    return session.scalar(sa.select(FetchFailure.round).where(FetchFailure.fetch_job_id == job_id))


def test_a_tool_call_in_the_loop_returns_metadata_only() -> None:
    from app.fetching.agent.tools import get_series_meta, search_catalog

    institution_code = _unique("inst")
    dataset_code = _unique("ds")
    with SessionLocal() as session:
        institution_id = _catalog(
            session, institution_code=institution_code, dataset_code=dataset_code, codes=("Y",)
        )
        dataset = session.scalar(
            sa.select(Dataset).where(
                Dataset.institution_id == institution_id,
                Dataset.external_code == dataset_code,
            )
        )
        series = Series(
            institution_id=institution_id,
            dataset_id=dataset.id,
            external_code=f"{dataset_code}:Y",
            name="test series",
            frequency="monthly",
            unit="TL",
        )
        session.add(series)
        session.flush()
        record_observations(
            session,
            series.id,
            [(date(2026, 1, 1), 1234)],
            fetched_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        session.commit()
        series_id = series.id
        job_id = _make_failed_job(
            session, institution_id=institution_id, dataset_code=dataset_code, code="Y"
        )

    client = ScriptedChatClient(
        _final("transient"),
        tool_calls=[
            tool_call(
                "c1",
                "get_series_meta",
                {
                    "institution": institution_code,
                    "dataset": dataset_code,
                    "codes": {"SERIE": "Y"},
                },
            )
        ],
    )
    result = service.diagnose_job(
        SessionLocal,
        job_id,
        chat_client_factory=lambda: client,
        max_rounds=2,
        dispatch=lambda _job: None,
    )
    assert result.outcome == "recorded"

    tool_messages = [
        message
        for call in client.calls
        for message in call["messages"]
        if message.get("role") == "tool"
    ]
    assert any("observation_count" in message["content"] for message in tool_messages)
    assert series_id is not None

    with SessionLocal() as session:
        meta = get_series_meta(
            session, institution=institution_code, dataset=dataset_code, codes={"SERIE": "Y"}
        )
        found = search_catalog(session, query=dataset_code, institution=institution_code)
    assert meta["observation_count"] == 1
    assert "value" not in meta
    assert set(meta) == {
        "exists",
        "external_code",
        "name",
        "unit",
        "frequency",
        "coverage_start",
        "coverage_end",
        "observation_count",
        "codes_valid",
        "invalid_codes",
    }
    assert meta["codes_valid"] is True
    assert found and found[0]["dataset"] == dataset_code
    assert "value" not in found[0]


def test_migration_0016_round_trips() -> None:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations" / "postgres"))
    # Do not hard-code the head: a later migration must not break this test.
    head = ScriptDirectory.from_config(config).get_current_head()

    engine = create_db_engine()
    try:
        assert "fetch_failures" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.downgrade(config, "0015")
    engine = create_db_engine()
    try:
        assert "fetch_failures" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_db_engine()
    try:
        assert "fetch_failures" in sa.inspect(engine).get_table_names()
        version = (
            engine.connect()
            .execute(sa.text("SELECT version_num FROM alembic_version"))
            .scalar_one()
        )
    finally:
        engine.dispose()
    assert version == head
