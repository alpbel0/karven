"""In-memory doubles for the Task 1.7 news-service unit tests.

``FakeStore``/``FakeSession`` implement the small set of session operations the
news service uses: ``scalar`` for ``SELECT`` and for the PostgreSQL
``INSERT ... ON CONFLICT DO NOTHING RETURNING id``, ``scalars`` for id lists,
plus ``add``/``flush``/``commit``/``rollback``/``get``. Only ``eq``/``in``/``is_``
and ``<`` predicates over ``news_articles`` are understood, which is exactly what
the service builds. ``FakeClient`` returns queued responses or raises a queued
exception, keyed by URL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import BooleanClauseList

from app.data.models import NewsArticle


def _flatten(criteria):
    for crit in criteria:
        if isinstance(crit, BooleanClauseList):
            yield from _flatten(crit.clauses)
        else:
            yield crit


def _literal(expr):
    return expr.value if hasattr(expr, "value") else expr


def _aggregate_column(statement) -> str | None:
    """The column name if ``statement`` selects ``func.min(<column>)``; else None."""
    for column in statement.selected_columns:
        if getattr(column, "name", None) != "min":
            continue
        clauses = list(getattr(column, "clauses", []))
        if clauses and hasattr(clauses[0], "name"):
            return clauses[0].name
    return None


def _matches(obj, statement) -> bool:
    for expr in _flatten(statement._where_criteria):
        column = expr.left.name
        operator = expr.operator
        right = _literal(expr.right)
        current = getattr(obj, column)
        name = operator.__name__
        if name in ("is_", "is"):
            if (right is None or type(right).__name__ == "Null") and current is not None:
                return False
            if right is not None and type(right).__name__ != "Null" and current is not right:
                return False
        elif name == "in_op":
            if current not in set(right):
                return False
        elif name == "eq":
            if current != right:
                return False
        elif name == "lt":
            if not current < right:
                return False
        else:
            raise AssertionError(f"unsupported operator {operator!r}")
    return True


class _Result:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeStore:
    """The shared ``news_articles`` table behind every :class:`FakeSession`."""

    def __init__(self) -> None:
        self.articles: list[NewsArticle] = []
        self.commits = 0
        self.flushes = 0
        self._ids = 0

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def add_article(self, article: NewsArticle) -> NewsArticle:
        if article.id is None:
            article.id = self.next_id()
        self.articles.append(article)
        return article


class FakeSession:
    """In-memory session over a shared :class:`FakeStore`."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    # --- unit of work -------------------------------------------------------
    def add(self, obj) -> None:
        if isinstance(obj, NewsArticle):
            self.store.add_article(obj)

    def flush(self) -> None:
        self.store.flushes += 1

    def commit(self) -> None:
        self.store.commits += 1

    def rollback(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # --- reads --------------------------------------------------------------
    def get(self, entity, ident):
        if entity is NewsArticle:
            return next((row for row in self.store.articles if row.id == ident), None)
        return None

    def scalar(self, statement):
        if isinstance(statement, Insert):
            return self._insert(statement)
        rows = [row for row in self.store.articles if _matches(row, statement)]
        column = _aggregate_column(statement)
        if column is not None:
            values = [getattr(row, column) for row in rows]
            values = [value for value in values if value is not None]
            return min(values) if values else None
        return rows[0] if rows else None

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        assert entity is NewsArticle
        rows = [row for row in self.store.articles if _matches(row, statement)]
        columns = list(statement.selected_columns)
        if (
            columns
            and hasattr(columns[0], "key")
            and columns[0].key in NewsArticle.__table__.columns
        ):
            values = [getattr(row, columns[0].key) for row in rows]
            return _Result(sorted(values))
        return _Result(rows)

    def _insert(self, statement) -> int | None:
        params = statement.compile(dialect=dialect()).params
        key = (params["source"], params["external_id"])
        if any((row.source, row.external_id) == key for row in self.store.articles):
            return None
        article = NewsArticle(**params)
        self.store.add_article(article)
        return article.id


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


Handler = Callable[[str], FakeResponse]


class FakeClient:
    """A stand-in for ``httpx.Client`` driven by a URL -> response handler."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[str] = []

    def get(self, url: str, headers=None, timeout=None) -> FakeResponse:
        self.calls.append(url)
        return self.handler(url)


def always(response: FakeResponse) -> Handler:
    return lambda _url: response


def raise_for(exc: Exception) -> Handler:
    def handler(_url: str) -> FakeResponse:
        raise exc

    return handler


@dataclass
class ClientScript:
    """Return/raise a scripted sequence per URL, then keep the last step."""

    steps: dict[str, list] = field(default_factory=dict)

    def handler(self, url: str) -> FakeResponse:
        queue = self.steps.get(url)
        if not queue:
            raise AssertionError(f"no scripted response for {url}")
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        return step


def make_article(
    store: FakeStore,
    *,
    source: str = "sabah",
    external_id: str = "https://example.com/a",
    url: str | None = None,
    title: str = "Title",
    text_status: str = "pending",
    content_text: str | None = None,
    fetched_at=None,
    first_seen_at=None,
    fetch_attempts: int = 0,
) -> NewsArticle:
    from datetime import UTC, datetime

    article = NewsArticle(
        source=source,
        external_id=external_id,
        url=url or external_id,
        title=title,
        text_status=text_status,
        content_text=content_text,
        fetched_at=fetched_at,
        first_seen_at=first_seen_at or datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
        fetch_attempts=fetch_attempts,
    )
    return store.add_article(article)


def connect_timeout() -> httpx.ConnectTimeout:
    return httpx.ConnectTimeout("timed out")
