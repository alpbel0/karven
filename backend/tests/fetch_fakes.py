"""In-memory session doubles shared by the Task 1.5 unit tests.

Real model instances live in per-entity lists on a :class:`Store`; a
:class:`FakeSession` implements the small set of operations the requests,
runner and watchdog use (``add``/``flush``/``commit``/``rollback``/``get``/
``scalar``/``scalars`` and ``execute`` for the status/heartbeat ``UPDATE``\\ s).
It is not a general database: only ``eq``/``in`` predicates over a single table
are understood, which is exactly what the code builds.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import BooleanClauseList

from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    FetchJob,
    FetchJobWaiter,
    Institution,
)

_TABLE_ENTITIES = {
    FetchJob.__tablename__: FetchJob,
    FetchJobWaiter.__tablename__: FetchJobWaiter,
    Institution.__tablename__: Institution,
    Dataset.__tablename__: Dataset,
    DatasetDimension.__tablename__: DatasetDimension,
    DimensionCode.__tablename__: DimensionCode,
}


def _flatten(criteria):
    for crit in criteria:
        if isinstance(crit, BooleanClauseList):
            yield from _flatten(crit.clauses)
        else:
            yield crit


def _literal(expr):
    return expr.value if hasattr(expr, "value") else expr


def _matches(obj, statement) -> bool:
    for expr in _flatten(statement._where_criteria):
        column = expr.left.name
        operator = expr.operator
        right = _literal(expr.right)
        if operator.__name__ == "in_op":
            if getattr(obj, column) not in set(right):
                return False
        elif operator.__name__ == "eq":
            if getattr(obj, column) != right:
                return False
        else:
            raise AssertionError(f"unsupported operator {operator!r}")
    return True


class _Result:
    """A tiny result set with ``all``/iteration and the UPDATE ``rowcount``."""

    def __init__(self, rows, *, rowcount: int | None = None) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Store:
    """One shared database behind every :class:`FakeSession` of a scenario."""

    def __init__(self) -> None:
        self.rows: dict[type, list] = {entity: [] for entity in _TABLE_ENTITIES.values()}
        self.commits = 0
        self._ids: dict[type, int] = {}

    def next_id(self, entity: type) -> int:
        self._ids[entity] = self._ids.get(entity, 0) + 1
        return self._ids[entity]


class FakeSession:
    """In-memory session over a shared :class:`Store`."""

    def __init__(self, store: Store, *, heartbeat_fail: bool = False) -> None:
        self.store = store
        self.heartbeat_fail = heartbeat_fail
        self.rollbacks = 0

    # --- unit of work -------------------------------------------------------

    def add(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = self.store.next_id(type(obj))
        self.store.rows[type(obj)].append(obj)

    def flush(self) -> None:
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

    # --- reads --------------------------------------------------------------

    def get(self, entity, ident):
        return next((row for row in self.store.rows[entity] if row.id == ident), None)

    @staticmethod
    def _entity(statement):
        return statement.column_descriptions[0]["entity"]

    def scalar(self, statement):
        entity = self._entity(statement)
        rows = [row for row in self.store.rows[entity] if _matches(row, statement)]
        return rows[0] if rows else None

    def scalars(self, statement):
        entity = self._entity(statement)
        return _Result(row for row in self.store.rows[entity] if _matches(row, statement))

    # --- update -------------------------------------------------------------

    def execute(self, statement):
        if isinstance(statement, Update):
            return self._execute_update(statement)
        raise AssertionError(f"unsupported statement {statement!r}")

    def _execute_update(self, statement):
        if self.heartbeat_fail and threading.current_thread().name.startswith("fetch-heartbeat"):
            raise RuntimeError("heartbeat database is down")
        entity = _TABLE_ENTITIES[statement.table.name]
        values = {
            getattr(col, "key", col): _literal(value) for col, value in statement._values.items()
        }
        matched = [row for row in self.store.rows[entity] if _matches(row, statement)]
        for row in matched:
            for key, value in values.items():
                setattr(row, key, value)
        return _Result([], rowcount=len(matched))


def factory(store: Store, **kwargs):
    """A session factory returning fresh sessions over one shared store."""

    def build() -> FakeSession:
        return FakeSession(store, **kwargs)

    return build


def make_institution(store: Store, code: str = "tcmb", name: str = "Fake TCMB") -> Institution:
    institution = Institution(id=store.next_id(Institution), code=code, name=name)
    store.rows[Institution].append(institution)
    return institution


def make_dataset(
    store: Store,
    institution: Institution,
    *,
    external_code: str = "DS_TEST",
    channel: str = "evds3",
    dimension_code: str = "SERIE",
    serie_code: str = "X",
    frequency: str = "monthly",
) -> Dataset:
    """A dataset with one non-time dimension carrying ``serie_code``."""
    dataset = Dataset(
        id=store.next_id(Dataset),
        institution_id=institution.id,
        external_code=external_code,
        name=f"Fake {external_code}",
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
    code = DimensionCode(
        id=store.next_id(DimensionCode),
        dimension_id=dimension.id,
        code=serie_code,
        label=serie_code,
    )
    dimension.codes.append(code)
    dataset.dimensions.append(dimension)
    store.rows[Dataset].append(dataset)
    store.rows[DatasetDimension].append(dimension)
    store.rows[DimensionCode].append(code)
    return dataset
