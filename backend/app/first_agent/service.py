"""The first agent (Task 3.3): read one news article, register candidates, select.

Flow: the agent reads the full text, uses the tools to find series and register
(a) news-visual candidates and (b) relation ideas, sends ready ideas to the graph
agent, and finishes with the final selection. The code owns every check
(:mod:`app.first_agent.tools`), the limits and the database write.

The agent never sees observation values: the tools return metadata only.

Empty model output is retried on the same request (2 more attempts, 3 in total;
the attempts are counted apart from the 60 tool calls). Any other failure of the
agent is recorded as ``agent_error`` and logged; the run is never retried
automatically (DECISIONS 4.6: the news goes to the failed-news list).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data.models import Dataset, DatasetTag, Institution, NewsArticle
from app.first_agent.persist import RunMeta, save_run
from app.first_agent.ports import GraphAgentPort, StubGraphAgent
from app.first_agent.schema import FINAL_SCHEMA, SYSTEM_PROMPT_KEY, Selection, parse_selection
from app.first_agent.state import MAX_TOOL_CALLS, RunState
from app.first_agent.tools import TOOL_LIMITS, ToolDeps, build_tools
from app.llm.errors import LLMOutputError
from app.llm.tool_loop import run_tool_loop
from app.prompts.errors import PromptNotFoundError
from app.prompts.ref import PromptRef
from app.prompts.service import activate, add_version, get_active

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

#: One request plus two retries when the model answers with nothing.
EMPTY_OUTPUT_ATTEMPTS = 3

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_DEFAULT_PROMPTS = ((SYSTEM_PROMPT_KEY, "first_agent_system.md"),)

_FINAL_INSTRUCTION = (
    "Now give your final answer as JSON that matches the required schema: your summary "
    "and the final selection of visual candidates and ideas (use only ids the tools "
    "returned; select an idea only if the graph agent answered it)."
)


class ChatClientLike(Protocol):
    """The subset of :class:`app.llm.chat.ChatClient` the agent uses."""

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        """Send one chat completion."""


@dataclass
class _MeteredClient:
    """Wraps the chat client: retries empty answers and meters every request.

    An answer with neither text nor tool calls is empty. Each retry is a new request
    with the same messages (the transcript is untouched). Usage, attempts and the
    requested tool-call count are kept so a failed run still records its cost.
    """

    inner: ChatClientLike
    attempts: int = 0
    tool_calls_requested: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None

    def complete(self, messages: Any, **kwargs: Any) -> Any:
        result = None
        for _ in range(EMPTY_OUTPUT_ATTEMPTS):
            result = self.inner.complete(messages, **kwargs)
            self.attempts += 1
            self._meter(result)
            if (result.content or "").strip() or result.tool_calls:
                break
            logger.warning("first agent: empty model output (attempt %s)", self.attempts)
        return result

    def _meter(self, result: Any) -> None:
        self.provider = getattr(result, "provider", None) or self.provider
        self.model = getattr(result, "model", None) or self.model
        self.tool_calls_requested += len(result.tool_calls or [])
        for key, value in (result.usage or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.usage[key] = self.usage.get(key, 0) + value

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()


@dataclass(frozen=True)
class FirstAgentResult:
    """What :func:`run_first_agent` returns (the full record is in the database)."""

    run_id: int
    status: str  # ok | agent_error
    news_id: int
    error: str | None
    summary: str | None
    selected_visuals: list[int]
    selected_ideas: list[int]
    visuals: int
    ideas: int
    catalog_gaps: int
    llm_attempts: int
    tool_calls: int
    usage: dict[str, Any]


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


# --- prompt -----------------------------------------------------------------


def ensure_default_prompt(session: Session) -> None:
    """Seed the default body for keys with no active version (never overwrites)."""
    for key, filename in _DEFAULT_PROMPTS:
        try:
            get_active(session, key)
            continue
        except PromptNotFoundError:
            pass
        body = (_PROMPT_DIR / filename).read_text(encoding="utf-8")
        version = add_version(session, key=key, body=body, note="Task 3.3 default")
        activate(session, key=key, version=version.version)
    session.flush()


def _prompt_view(session: Session, key: str):
    ensure_default_prompt(session)
    session.commit()
    return get_active(session, key)


# --- default dependencies ---------------------------------------------------


def accepted_leaf_tags(session: Session, institution: str, dataset: str) -> set[str]:
    """The accepted leaf concept ids of one dataset."""
    rows = session.scalars(
        sa.select(DatasetTag.tag_id)
        .join(Dataset, DatasetTag.dataset_id == Dataset.id)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(
            Institution.code == institution,
            Dataset.external_code == dataset,
            DatasetTag.status == "accepted",
            DatasetTag.level == "leaf",
        )
    ).all()
    return set(rows)


def _graph_lookup(target: Any, drivers: list[Any]) -> Any:
    from app.graph import graph_client, repository

    with graph_client() as session:
        return repository.find_relation(session, target, drivers)


def _fetch_question(question: str, context: dict[str, Any] | None) -> dict[str, Any]:
    from app.fetching.agent import ask_fetch_agent

    answer = ask_fetch_agent(question, context)
    return {
        "answer": answer.answer,
        "data_status": answer.data_status,
        "suggestion": answer.suggestion,
    }


def default_deps(
    session_factory: SessionFactory, graph_agent: GraphAgentPort | None = None
) -> tuple[ToolDeps, Callable[[], None]]:
    """The real tool dependencies and a ``close`` for the clients they opened."""
    from app.catalog.search import build_search_tool
    from app.catalog.status import build_status_tool, data_status
    from app.catalog.tree import load_tree
    from app.config import settings
    from app.llm.chat import ChatClient
    from app.llm.jev import JevClient

    jev = JevClient(settings)
    chat = ChatClient(settings)

    def status_fn(recipes: list[dict[str, Any]]) -> dict[str, Any]:
        with session_factory() as session:
            return data_status(session, recipes)

    def tags_fn(institution: str, dataset: str) -> set[str]:
        with session_factory() as session:
            return accepted_leaf_tags(session, institution, dataset)

    search_tools = {
        **build_search_tool(session_factory, jev, chat),
        **build_status_tool(session_factory),
    }
    deps = ToolDeps(
        status_fn=status_fn,
        tags_fn=tags_fn,
        graph_lookup=_graph_lookup,
        graph_agent=graph_agent or StubGraphAgent(),
        fetch_fn=_fetch_question,
        tree=load_tree(),
        search_tools=search_tools,
    )

    def close() -> None:
        jev.close()
        chat.close()

    return deps, close


# --- run --------------------------------------------------------------------


def _user_message(news: NewsArticle) -> str:
    published = news.published_at.isoformat() if news.published_at else "unknown"
    return (
        f"Source: {news.source}\nTitle: {news.title}\nPublished: {published}\n\n"
        f"Full text:\n{news.content_text}"
    )


def _counted(tools: dict[str, Any], counts: dict[str, int]) -> dict[str, Any]:
    """Wrap every tool so the run records how often each one was called."""

    def wrap(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def counted(*args: Any, **kwargs: Any) -> Any:
            counts[name] = counts.get(name, 0) + 1
            return function(*args, **kwargs)

        return counted

    return {name: (schema, wrap(name, function)) for name, (schema, function) in tools.items()}


def _apply_selection(state: RunState, selection: Selection) -> None:
    for local_id, reason in selection.visuals.items():
        visual = state.visual(local_id)
        if visual is not None:
            visual.selected = True
            visual.selection_reason = reason
    for local_id, reason in selection.ideas.items():
        idea = state.idea(local_id)
        if idea is not None:
            idea.selected = True
            idea.selection_reason = reason


def run_first_agent(
    news_id: int,
    *,
    session_factory: SessionFactory | None = None,
    chat_client_factory: Callable[[], ChatClientLike] | None = None,
    deps_factory: Callable[[SessionFactory], tuple[ToolDeps, Callable[[], None]]] | None = None,
) -> FirstAgentResult:
    """Run the first agent on one news article and record the run.

    Raises ``ValueError`` when the article does not exist or has no usable text
    (nothing is recorded: there was no run). Every failure after the run started
    is recorded as ``agent_error``.
    """
    factory = session_factory or _default_session_factory()
    client_factory = chat_client_factory or default_chat_client_factory
    with factory() as session:
        news = session.get(NewsArticle, news_id)
        if news is None:
            raise ValueError(f"news {news_id} does not exist")
        if news.text_status != "ok" or not news.content_text:
            raise ValueError(f"news {news_id} has no usable text (status {news.text_status})")
        user_message = _user_message(news)
        view = _prompt_view(session, SYSTEM_PROMPT_KEY)

    state = RunState(news_id=news_id)
    client = _MeteredClient(client_factory())
    deps, close_deps = (deps_factory or default_deps)(factory)
    prompt_ref = PromptRef(key=view.key, version=view.version, checksum=view.checksum)
    error: str | None = None
    selection: Selection | None = None
    loop_tool_calls: int | None = None
    tool_counts: dict[str, int] = {}
    try:
        result = run_tool_loop(
            client,
            [
                {"role": "system", "content": view.body},
                {"role": "user", "content": user_message},
            ],
            _counted(build_tools(state, deps), tool_counts),
            FINAL_SCHEMA,
            max_tool_calls=MAX_TOOL_CALLS,
            tool_limits=TOOL_LIMITS,
            final_instruction=_FINAL_INSTRUCTION,
            prompt_ref=prompt_ref,
        )
        loop_tool_calls = result.tool_calls
        selection = parse_selection(result.output, state)
        _apply_selection(state, selection)
    except Exception as exc:  # noqa: BLE001 - recorded as agent_error, never hidden
        error = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, LLMOutputError):
            logger.error("first agent output error for news %s: %s", news_id, error)
        else:
            logger.exception("first agent failed for news %s", news_id)
    finally:
        close_deps()
        client.close()

    meta = RunMeta(
        status="ok" if error is None else "agent_error",
        error=error,
        summary=selection.summary if selection else None,
        selection=({"visuals": selection.visuals, "ideas": selection.ideas} if selection else {}),
        prompt_key=view.key,
        prompt_version=view.version,
        prompt_checksum=view.checksum,
        provider=client.provider,
        model=client.model,
        usage={**client.usage, "tool_counts": dict(tool_counts)},
        llm_attempts=client.attempts,
        tool_calls=loop_tool_calls if loop_tool_calls is not None else client.tool_calls_requested,
    )
    with factory() as session:
        run_id = save_run(session, state, meta)
        session.commit()
    gaps = sum(
        1
        for slot in [
            *(visual.slot for visual in state.visuals),
            *(slot for idea in state.ideas for slot in idea.slots),
        ]
        if slot.is_gap
    )
    return FirstAgentResult(
        run_id=run_id,
        status=meta.status,
        news_id=news_id,
        error=error,
        summary=meta.summary,
        selected_visuals=sorted(selection.visuals) if selection else [],
        selected_ideas=sorted(selection.ideas) if selection else [],
        visuals=len(state.visuals),
        ideas=len(state.ideas),
        catalog_gaps=gaps,
        llm_attempts=meta.llm_attempts,
        tool_calls=meta.tool_calls,
        usage=meta.usage,
    )


__all__ = [
    "EMPTY_OUTPUT_ATTEMPTS",
    "FirstAgentResult",
    "accepted_leaf_tags",
    "default_chat_client_factory",
    "default_deps",
    "ensure_default_prompt",
    "run_first_agent",
]
