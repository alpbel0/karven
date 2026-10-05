"""Unit tests for the "loaded?" query helper (no database)."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.catalog.status import loaded_series_query, series_loaded


class _FakeResult:
    def __init__(self, rows: list[int]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[int]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[int]) -> None:
        self.rows = rows
        self.statement = None

    def execute(self, statement):  # noqa: ANN001 - test double
        self.statement = statement
        return _FakeResult(self.rows)


def test_query_is_one_grouped_statement() -> None:
    statement = loaded_series_query([1, 2, 3])
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "GROUP BY" in sql.upper()
    assert sql.upper().count("SELECT") == 1
    assert "series_id" in sql


def test_series_loaded_maps_every_requested_id() -> None:
    session = _FakeSession([1, 3])
    assert series_loaded(session, [1, 2, 3]) == {1: True, 2: False, 3: True}
    assert session.statement is not None


def test_series_loaded_empty_short_circuits() -> None:
    session = _FakeSession([])
    assert series_loaded(session, []) == {}
    assert session.statement is None
