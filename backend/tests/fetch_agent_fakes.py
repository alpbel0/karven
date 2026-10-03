"""In-memory doubles for the Task 1.6 agent unit tests.

A :class:`Store` holds real model instances per entity; :class:`FakeSession`
implements the small query surface the agent, its tools and ``request_fetch``
use (``get``/``add``/``flush``/``commit``/``rollback``/``scalar``/``scalars`` and
``execute`` for UPDATEs). Only ``eq``/``in``/``ilike`` predicates and a count
aggregate are understood -- exactly what the code builds. No network, no
database.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import BooleanClauseList

from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    FetchFailure,
    FetchJob,
    FetchJobWaiter,
    Institution,
    Observation,
    Series,
)
from app.prompts.models import ActivePrompt, PromptVersion

_ENTITIES = (
    Institution,
    Dataset,
    DatasetDimension,
    DimensionCode,
    Series,
    Observation,
    FetchJob,
    FetchJobWaiter,
    FetchFailure,
    PromptVersion,
    ActivePrompt,
)
_TABLE_ENTITIES = {entity.__tablename__: entity for entity in _ENTITIES}


def _literal(expr):
    return expr.value if hasattr(expr, "value") else expr


def _evaluate(expr, obj) -> bool:
    if isinstance(expr, BooleanClauseList):
        matches = [_evaluate(clause, obj) for clause in expr.clauses]
        if expr.operator is operators.or_:
            return any(matches)
        return all(matches)
    column = expr.left.name
    operator = expr.operator
    right = _literal(expr.right)
    actual = getattr(obj, column)
    name = operator.__name__
    if name == "in_op":
        return actual in set(right)
    if name == "eq":
        return actual == right
    if name == "ilike_op":
        needle = str(right).strip("%").lower()
        return needle in str(actual or "").lower()
    raise AssertionError(f"unsupported operator {operator!r}")


def _matches(obj, statement) -> bool:
    return all(_evaluate(expr, obj) for expr in statement._where_criteria)


class _Result:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Store:
    """One shared database behind every :class:`FakeSession` of a scenario."""

    def __init__(self) -> None:
        self.rows: dict[type, list] = {entity: [] for entity in _ENTITIES}
        self.commits = 0
        self._ids: dict[type, int] = {}

    def next_id(self, entity: type) -> int:
        self._ids[entity] = self._ids.get(entity, 0) + 1
        return self._ids[entity]


class FakeSession:
    """In-memory session over a shared :class:`Store`."""

    def __init__(self, store: Store, *, fail_failure_flush: bool = False) -> None:
        self.store = store
        self.rollbacks = 0
        self.fail_failure_flush = fail_failure_flush
        self._failure_pending = False

    def add(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = self.store.next_id(type(obj))
        self.store.rows[type(obj)].append(obj)
        if self.fail_failure_flush and isinstance(obj, FetchFailure):
            self._failure_pending = True

    def flush(self) -> None:
        if self._failure_pending:
            # Simulate the unique (fetch_job_id, round) violation raised on the
            # second concurrent delivery.
            self._failure_pending = False
            raise IntegrityError("insert fetch_failures", {}, Exception("duplicate"))
        return None

    def commit(self) -> None:
        self.store.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    @contextmanager
    def begin_nested(self):
        yield self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, entity, ident):
        return next((row for row in self.store.rows[entity] if row.id == ident), None)

    @staticmethod
    def _entity(statement):
        return statement.column_descriptions[0]["entity"]

    def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is None:
            entity = _TABLE_ENTITIES[statement.get_final_froms()[0].name]
            return len([row for row in self.store.rows[entity] if _matches(row, statement)])
        rows = [row for row in self.store.rows[entity] if _matches(row, statement)]
        return rows[0] if rows else None

    def scalars(self, statement):
        entity = self._entity(statement)
        return _Result(row for row in self.store.rows[entity] if _matches(row, statement))

    def execute(self, statement):
        if isinstance(statement, Update):
            entity = _TABLE_ENTITIES[statement.table.name]
            values = {
                getattr(col, "key", col): _literal(value)
                for col, value in statement._values.items()
            }
            matched = [row for row in self.store.rows[entity] if _matches(row, statement)]
            for row in matched:
                for key, value in values.items():
                    setattr(row, key, value)
            return _Result(matched)
        raise AssertionError(f"unsupported statement {statement!r}")


def factory(store: Store, **kwargs):
    def build() -> FakeSession:
        return FakeSession(store, **kwargs)

    return build


# --- catalog helpers --------------------------------------------------------


def make_institution(store: Store, code: str = "tcmb", name: str | None = None) -> Institution:
    institution = Institution(id=store.next_id(Institution), code=code, name=name or code)
    store.rows[Institution].append(institution)
    return institution


def make_dataset(
    store: Store,
    institution: Institution,
    *,
    external_code: str = "DS_TEST",
    name: str | None = None,
    channel: str | None = "evds3",
    dimension_code: str = "SERIE",
    series_codes: tuple[str, ...] = ("X",),
    frequency: str = "monthly",
    series: bool = False,
) -> Dataset:
    dataset = Dataset(
        id=store.next_id(Dataset),
        institution_id=institution.id,
        external_code=external_code,
        name=name or f"Fake {external_code}",
        attributes={"channel": channel, "default_frequency": frequency},
    )
    dimension = DatasetDimension(
        id=store.next_id(DatasetDimension),
        dataset_id=dataset.id,
        code=dimension_code,
        label=dimension_code,
        position=0,
        role="other",
    )
    for code in series_codes:
        row = DimensionCode(
            id=store.next_id(DimensionCode),
            dimension_id=dimension.id,
            code=code,
            label=code,
        )
        dimension.codes.append(row)
        store.rows[DimensionCode].append(row)
    dataset.dimensions.append(dimension)
    store.rows[Dataset].append(dataset)
    store.rows[DatasetDimension].append(dimension)
    return dataset


def make_series(
    store: Store,
    institution: Institution,
    dataset: Dataset,
    *,
    external_code: str,
    name: str = "Fake series",
    frequency: str = "monthly",
    unit: str | None = "TL",
    coverage_start: date | None = None,
    coverage_end: date | None = None,
) -> Series:
    series = Series(
        id=store.next_id(Series),
        institution_id=institution.id,
        dataset_id=dataset.id,
        external_code=external_code,
        name=name,
        frequency=frequency,
        unit=unit,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    store.rows[Series].append(series)
    return series


def add_waiter(
    store: Store,
    job: FetchJob,
    *,
    waiter_type: str = "idea",
    waiter_id: str = "1",
    status: str = "dropped",
) -> FetchJobWaiter:
    waiter = FetchJobWaiter(
        id=store.next_id(FetchJobWaiter),
        fetch_job_id=job.id,
        waiter_type=waiter_type,
        waiter_id=waiter_id,
        status=status,
    )
    job.waiters.append(waiter)
    store.rows[FetchJobWaiter].append(waiter)
    return waiter


def make_observation(store: Store, series: Series, period: date, value: Any = 1) -> Observation:
    observation = Observation(
        id=store.next_id(Observation),
        series_id=series.id,
        period=period,
        value=value,
    )
    store.rows[Observation].append(observation)
    return observation


def make_failed_job(
    store: Store,
    institution: Institution,
    *,
    external_code: str = "DS_TEST:X",
    dataset_code: str | None = "DS_TEST",
    codes: dict[str, str] | None = None,
    start: date = date(2000, 1, 1),
    error_reason: str = "not_found: no such series",
    origin: str = "on_demand",
    agent_round: int | None = None,
    parent_job_id: int | None = None,
    series_id: int | None = None,
) -> FetchJob:
    job = FetchJob(
        id=store.next_id(FetchJob),
        institution_id=institution.id,
        external_code=external_code,
        series_id=series_id,
        status="failed",
        error_reason=error_reason,
        attributes={
            "origin": origin,
            "dataset_code": dataset_code,
            "codes": codes or {"SERIE": "Y"},
            "start": start.isoformat(),
        },
    )
    if agent_round is not None:
        job.attributes["agent_round"] = agent_round
    if parent_job_id is not None:
        job.attributes["parent_job_id"] = parent_job_id
    store.rows[FetchJob].append(job)
    return job


# --- chat client ------------------------------------------------------------


@dataclass
class _ChatResult:
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    usage: dict[str, Any]
    provider: str
    model: str


class ScriptedChatClient:
    """A chat client that returns a tool turn then a final JSON object.

    ``final`` is the JSON object (or raw string) returned on the
    ``response_format`` call. ``raise_exc`` makes every call raise instead.
    """

    def __init__(
        self,
        final: Any,
        *,
        first_content: str | None = "thinking",
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
        provider: str = "fake",
        model: str = "fake-model",
        raise_exc: BaseException | None = None,
    ) -> None:
        self.final = final
        self.first_content = first_content
        self.tool_calls = tool_calls
        self.usage = usage if usage is not None else {"prompt_tokens": 5, "completion_tokens": 7}
        self.provider = provider
        self.model = model
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self._tool_turn_done = False

    def complete(self, messages, *, tools=None, response_format=None, prompt_ref=None, **kwargs):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "response_format": response_format,
                "prompt_ref": prompt_ref,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        if response_format is not None:
            content = self.final if isinstance(self.final, str) else json.dumps(self.final)
            return _ChatResult(content, None, self.usage, self.provider, self.model)
        if self.tool_calls and not self._tool_turn_done:
            self._tool_turn_done = True
            return _ChatResult(None, self.tool_calls, self.usage, self.provider, self.model)
        return _ChatResult(self.first_content, None, self.usage, self.provider, self.model)

    def close(self) -> None:
        self.closed = True


__all__ = [
    "FakeSession",
    "ScriptedChatClient",
    "Store",
    "add_waiter",
    "factory",
    "make_dataset",
    "make_failed_job",
    "make_institution",
    "make_observation",
    "make_series",
]
