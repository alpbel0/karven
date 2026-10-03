"""The data-fetch agent (Task 1.6): diagnose failures, answer questions.

The agent never sees observation values; :mod:`app.fetching.agent.tools` returns
metadata only. The agent only diagnoses and proposes -- the code here validates
the proposal (retry category, round limit, catalogued dataset, valid codes,
non-identical request) and executes it. Monitoring, status changes and waiting
are code's job (Task 1.5).

``diagnose_job`` writes exactly one :class:`~app.data.models.FetchFailure` per
``(job_id, round)`` so a re-delivered Celery task is a no-op. Any failure of the
agent itself (LLM or output parsing) is recorded as ``outcome='agent_error'`` and
logged, never hidden and never retried.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.base import build_series_definition
from app.core.loader import find_dataset
from app.data import fetch_jobs
from app.data.errors import SeriesDefinitionError
from app.data.fetch_requests import (
    AGENT_RETRY,
    DEFAULT_START,
    WATCHED_ORIGINS,
    request_fetch,
)
from app.data.models import FetchFailure, FetchJob, Institution
from app.fetching.agent.schema import (
    ASK_FINAL_SCHEMA,
    ASK_PROMPT_KEY,
    DIAGNOSE_FINAL_SCHEMA,
    RETRYABLE_CATEGORIES,
    SYSTEM_PROMPT_KEY,
    AskAnswer,
    Diagnosis,
    parse_ask_answer,
    parse_diagnosis,
)
from app.fetching.agent.tools import build_tools
from app.llm.errors import LLMError
from app.llm.tool_loop import run_tool_loop
from app.prompts.errors import PromptNotFoundError
from app.prompts.ref import PromptRef
from app.prompts.service import activate, add_version, get_active

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
Dispatch = Callable[[int], None]

MAX_TOOL_CALLS = 8
MAX_ROUNDS_FLOOR = 1

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_DEFAULT_PROMPTS = (
    (SYSTEM_PROMPT_KEY, "fetch_agent_system.md"),
    (ASK_PROMPT_KEY, "fetch_agent_ask.md"),
)


class ChatClientLike(Protocol):
    """The subset of :class:`app.llm.chat.ChatClient` the agent uses."""

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        """Send one chat completion."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def default_chat_client_factory() -> ChatClientLike:
    """Build the real chat client (EVREN first, OpenRouter fallback)."""
    from app.config import settings
    from app.llm.chat import ChatClient
    from app.llm.raw_log import build_raw_logger

    raw_logger = build_raw_logger(settings) if settings.minio_bucket_raw else None
    return ChatClient(settings, raw_logger=raw_logger)


def _default_session_factory() -> SessionFactory:
    from app.db.session import SessionLocal

    return SessionLocal


def _close_client(client: ChatClientLike | None) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def ensure_default_prompt(session: Session) -> None:
    """Seed the default bodies for keys with no active version.

    Code seeds a prompt only when no version is active; an admin-added version is
    never overwritten. The caller commits the pending rows.
    """
    for key, filename in _DEFAULT_PROMPTS:
        try:
            get_active(session, key)
            continue
        except PromptNotFoundError:
            pass
        body = (_PROMPT_DIR / filename).read_text(encoding="utf-8")
        version = add_version(session, key=key, body=body, note="Task 1.6 default")
        activate(session, key=key, version=version.version)
    session.flush()


def _prompt_view(session: Session, key: str):
    """The active prompt after seeding the defaults, or None."""
    ensure_default_prompt(session)
    session.commit()
    try:
        return get_active(session, key)
    except PromptNotFoundError:  # pragma: no cover - only if the DB lost the rows
        return None


def _parse_start(value: Any) -> date:
    if not value:
        return DEFAULT_START
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return DEFAULT_START


# --- diagnose ---------------------------------------------------------------


@dataclass(frozen=True)
class _JobContext:
    """Everything the agent may see about one failed job (metadata only)."""

    job_id: int
    institution_id: int
    institution_code: str
    external_code: str
    series_id: int | None
    dataset_code: str | None
    codes: dict[str, str]
    start: date
    error_reason: str
    round: int
    previous: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DiagnoseResult:
    """Outcome of one :func:`diagnose_job` call."""

    outcome: str  # skipped | recorded | retry_requested | agent_error
    job_id: int
    failure_id: int | None = None
    category: str | None = None
    retry_job_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _RetryDecision:
    accepted: bool
    reason: str | None = None
    institution: str = ""
    dataset: str = ""
    codes: dict[str, str] = field(default_factory=dict)


def _previous_diagnoses(session: Session, attributes: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Diagnoses of the parent chain, oldest first (bounded walk)."""
    chain: list[dict[str, Any]] = []
    parent_id = attributes.get("parent_job_id")
    depth = 0
    while parent_id is not None and depth < 10:
        parent = session.get(FetchJob, int(parent_id))
        if parent is None:
            break
        rows = session.scalars(
            sa.select(FetchFailure).where(FetchFailure.fetch_job_id == parent.id)
        ).all()
        for row in rows:
            chain.append(
                {
                    "round": row.round,
                    "category": row.category,
                    "diagnosis": row.diagnosis,
                    "suggestion": row.suggestion,
                }
            )
        parent_id = (parent.attributes or {}).get("parent_job_id")
        depth += 1
    chain.reverse()
    return chain


def _load_context(session: Session, job_id: int) -> tuple[_JobContext | None, str]:
    """Load the job metadata, or ``(None, reason)`` when it must be skipped."""
    job = session.get(FetchJob, job_id)
    if job is None:
        return None, "job not found"
    attributes = dict(job.attributes or {})
    if job.status != fetch_jobs.FAILED:
        return None, "job is not failed"
    if attributes.get("origin") not in WATCHED_ORIGINS:
        return None, "origin is not diagnosed by the fetch agent"
    round_no = int(attributes.get("agent_round", 0) or 0) + 1
    existing = session.scalar(
        sa.select(FetchFailure).where(
            FetchFailure.fetch_job_id == job_id,
            FetchFailure.round == round_no,
        )
    )
    if existing is not None:
        return None, "already diagnosed"
    institution = session.get(Institution, job.institution_id)
    institution_code = institution.code if institution is not None else ""
    return (
        _JobContext(
            job_id=job_id,
            institution_id=job.institution_id,
            institution_code=institution_code,
            external_code=job.external_code,
            series_id=job.series_id,
            dataset_code=attributes.get("dataset_code"),
            codes=dict(attributes.get("codes") or {}),
            start=_parse_start(attributes.get("start")),
            error_reason=job.error_reason or "",
            round=round_no,
            previous=_previous_diagnoses(session, attributes),
        ),
        "",
    )


def _build_user_message(ctx: _JobContext) -> str:
    """The user message: job metadata only, never a value."""
    payload = {
        "institution": ctx.institution_code,
        "dataset_code": ctx.dataset_code,
        "codes": ctx.codes,
        "start": ctx.start.isoformat(),
        "error_reason": ctx.error_reason,
        "round": ctx.round,
        "previous_rounds": ctx.previous,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _insert_failure(
    session: Session,
    ctx: _JobContext,
    *,
    outcome: str,
    category: str,
    diagnosis: str | None,
    suggestion: str | None,
    alternatives: list[dict[str, Any]],
    retry_job_id: int | None,
    prompt_view: Any,
    provider: str | None,
    model: str | None,
    usage: dict[str, Any],
    agent_error: str | None,
    moment: datetime,
) -> FetchFailure | None:
    """Insert the one failure row for ``(job, round)``; None on a duplicate."""
    failure = FetchFailure(
        fetch_job_id=ctx.job_id,
        round=ctx.round,
        institution_id=ctx.institution_id,
        external_code=ctx.external_code,
        series_id=ctx.series_id,
        dataset_code=ctx.dataset_code,
        codes=ctx.codes,
        error_reason=ctx.error_reason or "",
        category=category,
        outcome=outcome,
        diagnosis=diagnosis,
        suggestion=suggestion,
        alternatives=alternatives,
        retry_job_id=retry_job_id,
        prompt_key=prompt_view.key if prompt_view is not None else None,
        prompt_version=prompt_view.version if prompt_view is not None else None,
        prompt_checksum=prompt_view.checksum if prompt_view is not None else None,
        llm_provider=provider,
        llm_model=model,
        llm_usage=usage or {},
        agent_error=agent_error,
        created_at=moment,
    )
    session.add(failure)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent delivery recorded this round first; stay idempotent.
        session.rollback()
        return None
    return failure


def _record_agent_error(
    session_factory: SessionFactory,
    ctx: _JobContext,
    exc: BaseException | str,
    prompt_view: Any,
    moment: datetime,
) -> DiagnoseResult:
    text = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)
    logger.error(
        "fetch agent failed for job %s: %s",
        ctx.job_id,
        text,
        exc_info=isinstance(exc, BaseException),
    )
    with session_factory() as session:
        failure = _insert_failure(
            session,
            ctx,
            outcome="agent_error",
            category="unknown",
            diagnosis=None,
            suggestion=None,
            alternatives=[],
            retry_job_id=None,
            prompt_view=prompt_view,
            provider=None,
            model=None,
            usage={},
            agent_error=text,
            moment=moment,
        )
        session.commit()
    return DiagnoseResult(
        "agent_error",
        ctx.job_id,
        failure_id=failure.id if failure is not None else None,
        category="unknown",
        reason=text,
    )


def _validate_retry(
    session_factory: SessionFactory,
    ctx: _JobContext,
    diagnosis: Diagnosis,
    max_rounds: int,
) -> _RetryDecision:
    """Code decides whether the proposed retry may be executed."""
    if diagnosis.category not in RETRYABLE_CATEGORIES:
        return _RetryDecision(
            False, f"'{diagnosis.category}' kategorisi yeniden denemeye uygun değil"
        )
    if ctx.round >= max_rounds:
        return _RetryDecision(False, f"tur sınırı ({max_rounds}) aşıldı")
    retry = diagnosis.retry
    if not retry:
        return _RetryDecision(False, "yeniden deneme önerilmedi")
    institution = str(retry["institution"])
    dataset_code = str(retry["dataset"])
    codes = {str(key): str(value) for key, value in retry["codes"].items()}
    with session_factory() as session:
        dataset = find_dataset(session, institution, dataset_code)
        if dataset is None:
            return _RetryDecision(
                False, f"'{dataset_code}' veri seti {institution} kataloğunda yok"
            )
        try:
            build_series_definition(dataset, codes)
        except SeriesDefinitionError as exc:
            return _RetryDecision(False, f"kodlar geçersiz: {exc}")
    identical = (
        institution == ctx.institution_code
        and dataset_code == ctx.dataset_code
        and codes == ctx.codes
    )
    if identical and diagnosis.category != "transient":
        return _RetryDecision(False, "aynı istek yinelenemez")
    return _RetryDecision(True, institution=institution, dataset=dataset_code, codes=codes)


def _append_reason(suggestion: str, reason: str) -> str:
    return f"{suggestion} (yeniden deneme reddedildi: {reason})".strip()


def _dispatch_retry(
    dispatch: Dispatch | None,
    job_id: int,
    session_factory: SessionFactory,
    moment: datetime,
) -> None:
    if dispatch is None:
        from app.fetching.tasks import enqueue_job

        dispatch = enqueue_job
    try:
        dispatch(job_id)
    except Exception:  # noqa: BLE001 - the committed job is safe; watchdog retries
        logger.exception("fetch agent retry job %s could not be enqueued", job_id)
        return
    with session_factory() as session:
        fetch_jobs.stamp_enqueued(session, job_id, now=moment)
        session.commit()


def _execute_retry(
    session_factory: SessionFactory,
    ctx: _JobContext,
    decision: _RetryDecision,
    diagnosis: Diagnosis,
    loop_result: Any,
    prompt_view: Any,
    moment: datetime,
    dispatch: Dispatch | None,
    dispatch_retry: bool,
) -> DiagnoseResult:
    with session_factory() as session:
        failed = session.get(FetchJob, ctx.job_id)
        request = request_fetch(
            session,
            institution_code=decision.institution,
            dataset_code=decision.dataset,
            codes=decision.codes,
            start=ctx.start,
        )
        target = session.get(FetchJob, request.job_id)
        if request.created:
            attributes = dict(target.attributes or {})
            attributes.update(
                {
                    "origin": AGENT_RETRY,
                    "agent_round": ctx.round,
                    "parent_job_id": ctx.job_id,
                }
            )
            target.attributes = attributes
        # Re-park the failed job's waiters on the retry (new or already active);
        # the old ``dropped`` rows stay as history. ``attach_waiter`` is idempotent.
        if failed is not None:
            for waiter in list(failed.waiters):
                fetch_jobs.attach_waiter(
                    session,
                    target,
                    waiter_type=waiter.waiter_type,
                    waiter_id=waiter.waiter_id,
                )
        failure = _insert_failure(
            session,
            ctx,
            outcome="retry_requested",
            category=diagnosis.category,
            diagnosis=diagnosis.diagnosis,
            suggestion=diagnosis.suggestion,
            alternatives=diagnosis.alternatives,
            retry_job_id=request.job_id,
            prompt_view=prompt_view,
            provider=loop_result.final.provider if loop_result is not None else None,
            model=loop_result.final.model if loop_result is not None else None,
            usage=dict(loop_result.usage or {}) if loop_result is not None else {},
            agent_error=None,
            moment=moment,
        )
        session.commit()
    if failure is None:
        # A concurrent delivery already recorded this (job, round): its rollback
        # also undid the retry job created above, so there is nothing to dispatch.
        return DiagnoseResult(
            "skipped",
            ctx.job_id,
            reason="already diagnosed (concurrent delivery)",
        )
    if request.created and dispatch_retry:
        _dispatch_retry(dispatch, request.job_id, session_factory, moment)
    return DiagnoseResult(
        "retry_requested",
        ctx.job_id,
        failure_id=failure.id,
        category=diagnosis.category,
        retry_job_id=request.job_id,
    )


def _act_on_diagnosis(
    session_factory: SessionFactory,
    ctx: _JobContext,
    diagnosis: Diagnosis,
    loop_result: Any,
    prompt_view: Any,
    max_rounds: int,
    dispatch: Dispatch | None,
    dispatch_retry: bool,
    moment: datetime,
) -> DiagnoseResult:
    decision = _validate_retry(session_factory, ctx, diagnosis, max_rounds)
    if decision.accepted:
        return _execute_retry(
            session_factory,
            ctx,
            decision,
            diagnosis,
            loop_result,
            prompt_view,
            moment,
            dispatch,
            dispatch_retry,
        )
    suggestion = _append_reason(diagnosis.suggestion, decision.reason or "")
    with session_factory() as session:
        failure = _insert_failure(
            session,
            ctx,
            outcome="recorded",
            category=diagnosis.category,
            diagnosis=diagnosis.diagnosis,
            suggestion=suggestion,
            alternatives=diagnosis.alternatives,
            retry_job_id=None,
            prompt_view=prompt_view,
            provider=loop_result.final.provider if loop_result is not None else None,
            model=loop_result.final.model if loop_result is not None else None,
            usage=dict(loop_result.usage or {}) if loop_result is not None else {},
            agent_error=None,
            moment=moment,
        )
        session.commit()
    return DiagnoseResult(
        "recorded",
        ctx.job_id,
        failure_id=failure.id if failure is not None else None,
        category=diagnosis.category,
        reason=decision.reason,
    )


def diagnose_job(
    session_factory: SessionFactory,
    job_id: int,
    *,
    chat_client_factory: Callable[[], ChatClientLike],
    max_rounds: int,
    dispatch: Dispatch | None = None,
    dispatch_retry: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> DiagnoseResult:
    """Diagnose one failed on-demand job; record exactly one failure row.

    ``dispatch`` is the enqueue callable used for a newly created retry (default
    ``enqueue_job``); ``dispatch_retry=False`` records the retry without
    dispatching it (the watchdog re-enqueues it later).
    """
    moment = (clock or _utcnow)()
    effective_rounds = max(int(max_rounds), MAX_ROUNDS_FLOOR)
    with session_factory() as session:
        ctx, reason = _load_context(session, job_id)
    if ctx is None:
        return DiagnoseResult("skipped", job_id, reason=reason)

    prompt_view = None
    client: ChatClientLike | None = None
    loop_result = None
    try:
        with session_factory() as session:
            prompt_view = _prompt_view(session, SYSTEM_PROMPT_KEY)
        client = chat_client_factory()
        tools = build_tools(session_factory)
        prompt_ref = (
            PromptRef(
                key=prompt_view.key,
                version=prompt_view.version,
                checksum=prompt_view.checksum,
            )
            if prompt_view is not None
            else None
        )
        messages = [
            {"role": "system", "content": prompt_view.body if prompt_view is not None else ""},
            {"role": "user", "content": _build_user_message(ctx)},
        ]
        loop_result = run_tool_loop(
            client,
            messages,
            tools,
            DIAGNOSE_FINAL_SCHEMA,
            max_tool_calls=MAX_TOOL_CALLS,
            prompt_ref=prompt_ref,
        )
        diagnosis = parse_diagnosis(loop_result.output)
    except LLMError as exc:
        _close_client(client)
        return _record_agent_error(session_factory, ctx, exc, prompt_view, moment)
    except Exception:
        _close_client(client)
        logger.exception("fetch agent diagnose crashed for job %s", job_id)
        raise
    _close_client(client)
    return _act_on_diagnosis(
        session_factory,
        ctx,
        diagnosis,
        loop_result,
        prompt_view,
        effective_rounds,
        dispatch,
        dispatch_retry,
        moment,
    )


# --- ask --------------------------------------------------------------------


@dataclass(frozen=True)
class FetchAgentAnswer:
    """The typed answer :func:`ask_fetch_agent` returns to the first/graph agent."""

    answer: str
    data_status: dict[str, Any]
    suggestion: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None


def ask_fetch_agent(
    question: str,
    context: dict[str, Any] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    chat_client_factory: Callable[[], ChatClientLike] | None = None,
) -> FetchAgentAnswer:
    """Answer a metadata question; opens no jobs and writes no fetch rows."""
    factory = session_factory or _default_session_factory()
    client_factory = chat_client_factory or default_chat_client_factory
    with factory() as session:
        view = _prompt_view(session, ASK_PROMPT_KEY)
    client = client_factory()
    try:
        tools = build_tools(factory)
        parts = [question]
        if context:
            parts.append("context: " + json.dumps(context, ensure_ascii=False, default=str))
        messages = [
            {"role": "system", "content": view.body if view is not None else ""},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        prompt_ref = (
            PromptRef(key=view.key, version=view.version, checksum=view.checksum)
            if view is not None
            else None
        )
        result = run_tool_loop(
            client,
            messages,
            tools,
            ASK_FINAL_SCHEMA,
            max_tool_calls=MAX_TOOL_CALLS,
            prompt_ref=prompt_ref,
        )
    finally:
        _close_client(client)
    parsed: AskAnswer = parse_ask_answer(result.output)
    return FetchAgentAnswer(
        answer=parsed.answer,
        data_status=parsed.data_status,
        suggestion=parsed.suggestion,
        usage=dict(result.usage or {}),
        provider=result.final.provider,
        model=result.final.model,
    )


# --- admin list -------------------------------------------------------------


def list_failures(
    session: Session,
    *,
    limit: int = 50,
    outcome: str | None = None,
) -> list[dict[str, Any]]:
    """Recent failure rows as plain dicts (Task 5.3's admin list calls this)."""
    cap = max(1, int(limit or 50))
    statement = sa.select(FetchFailure).order_by(FetchFailure.id.desc()).limit(cap)
    if outcome is not None:
        statement = statement.where(FetchFailure.outcome == outcome)
    rows = session.scalars(statement).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        institution = session.get(Institution, row.institution_id)
        result.append(
            {
                "id": row.id,
                "job_id": row.fetch_job_id,
                "round": row.round,
                "institution": institution.code if institution is not None else None,
                "external_code": row.external_code,
                "dataset_code": row.dataset_code,
                "codes": dict(row.codes or {}),
                "category": row.category,
                "outcome": row.outcome,
                "diagnosis": row.diagnosis,
                "suggestion": row.suggestion,
                "alternatives": list(row.alternatives or []),
                "retry_job_id": row.retry_job_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return result


__all__ = [
    "DiagnoseResult",
    "FetchAgentAnswer",
    "ask_fetch_agent",
    "default_chat_client_factory",
    "diagnose_job",
    "ensure_default_prompt",
    "list_failures",
]
