"""Unit tests for alert open/resolve idempotency, using a tiny in-memory session.

The real database (and the partial unique index) is exercised in the integration
suite; here a hand-rolled session evaluates the small set of ``SELECT`` shapes
:mod:`app.core.alerts` builds, so the idempotency contract is pinned without a DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.sql.elements import BooleanClauseList, UnaryExpression

from app.core.alerts import (
    FORMAT_CHANGED,
    NO_NEW_PERIOD,
    REPEATED_FAILURE,
    list_alerts,
    open_alert,
    resolve_alerts,
)
from app.data.models import DataAlert


def _flatten(criteria):
    for crit in criteria:
        if isinstance(crit, BooleanClauseList):
            yield from _flatten(crit.clauses)
        else:
            yield crit


def _matches(obj, statement) -> bool:
    for expr in _flatten(statement._where_criteria):
        if isinstance(expr, UnaryExpression):
            raise AssertionError(f"unsupported predicate {expr!r}")
        column = expr.left.name
        operator = expr.operator
        right = expr.right
        if operator.__name__ == "in_op":
            values = set(right.value)
            if getattr(obj, column) not in values:
                return False
        elif operator.__name__ == "eq":
            if getattr(obj, column) != right.value:
                return False
        else:
            raise AssertionError(f"unsupported operator {operator!r}")
    return True


class FakeSession:
    """Minimal Session double: add/flush/scalar/scalars over a list of rows."""

    def __init__(self) -> None:
        self.rows: list[DataAlert] = []

    def add(self, obj: DataAlert) -> None:
        if obj.id is None:
            obj.id = len(self.rows) + 1
        self.rows.append(obj)

    def flush(self) -> None:
        return None

    def scalar(self, statement):
        found = [row for row in self.rows if _matches(row, statement)]
        return found[0] if found else None

    def scalars(self, statement):
        return _FakeResult([row for row in self.rows if _matches(row, statement)])


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


def test_open_alert_creates_one_then_refreshes_in_place() -> None:
    session = FakeSession()
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    first = open_alert(
        session,
        institution_id=1,
        scope="bie_cli2:TP.CLI2.A01",
        kind=NO_NEW_PERIOD,
        message="first",
        detail={"a": 1},
        series_id=7,
        now=now,
    )
    later = datetime(2026, 11, 1, 12, tzinfo=UTC)
    second = open_alert(
        session,
        institution_id=1,
        scope="bie_cli2:TP.CLI2.A01",
        kind=NO_NEW_PERIOD,
        message="second",
        detail={"a": 2},
        series_id=7,
        now=later,
    )
    assert first is second
    assert len(session.rows) == 1
    assert second.message == "second"
    assert second.detail == {"a": 2}
    assert second.last_seen_at == later
    assert second.opened_at == now


def test_open_alert_keeps_open_rows_separate_per_kind() -> None:
    session = FakeSession()
    now = datetime(2026, 10, 30, tzinfo=UTC)
    open_alert(session, institution_id=1, scope="s", kind=NO_NEW_PERIOD, message="a", now=now)
    open_alert(session, institution_id=1, scope="s", kind=REPEATED_FAILURE, message="b", now=now)
    assert len(session.rows) == 2


def test_resolve_alerts_closes_only_matching_kinds() -> None:
    session = FakeSession()
    now = datetime(2026, 10, 30, tzinfo=UTC)
    open_alert(session, institution_id=1, scope="s", kind=NO_NEW_PERIOD, message="a", now=now)
    open_alert(session, institution_id=1, scope="s", kind=FORMAT_CHANGED, message="b", now=now)

    closed = resolve_alerts(
        session, institution_id=1, scope="s", kinds=[NO_NEW_PERIOD], now=now
    )
    assert closed == 1
    remaining = [row for row in session.rows if row.status == "open"]
    assert [row.kind for row in remaining] == [FORMAT_CHANGED]


def test_resolve_is_idempotent() -> None:
    session = FakeSession()
    now = datetime(2026, 10, 30, tzinfo=UTC)
    open_alert(session, institution_id=1, scope="s", kind=NO_NEW_PERIOD, message="a", now=now)
    assert resolve_alerts(session, institution_id=1, scope="s", now=now) == 1
    assert resolve_alerts(session, institution_id=1, scope="s", now=now) == 0


def test_reopening_after_resolve_creates_a_new_row() -> None:
    session = FakeSession()
    now = datetime(2026, 10, 30, tzinfo=UTC)
    first = open_alert(
        session, institution_id=1, scope="s", kind=NO_NEW_PERIOD, message="a", now=now
    )
    resolve_alerts(session, institution_id=1, scope="s", now=now)
    second = open_alert(
        session, institution_id=1, scope="s", kind=NO_NEW_PERIOD, message="b", now=now
    )
    assert first is not second
    assert len(session.rows) == 2
    assert {row.status for row in session.rows} == {"open", "resolved"}


def test_list_alerts_filters_by_status() -> None:
    session = FakeSession()
    now = datetime(2026, 10, 30, tzinfo=UTC)
    open_alert(session, institution_id=1, scope="a", kind=NO_NEW_PERIOD, message="a", now=now)
    open_alert(session, institution_id=1, scope="b", kind=NO_NEW_PERIOD, message="b", now=now)
    resolve_alerts(session, institution_id=1, scope="a", now=now)
    assert {row.scope for row in list_alerts(session, statuses={"open"})} == {"b"}
    assert len(list_alerts(session, statuses={"open", "resolved"})) == 2


def test_unknown_kind_is_rejected() -> None:
    session = FakeSession()
    with pytest.raises(ValueError):
        open_alert(session, institution_id=1, scope="s", kind="bogus", message="x")


def test_select_shape_is_supported_by_the_fake() -> None:
    """Guard: the fake understands the same ``select`` the module writes."""
    session = FakeSession()
    now = datetime(2026, 10, 30, tzinfo=UTC)
    alert = open_alert(
        session, institution_id=3, scope="x", kind=NO_NEW_PERIOD, message="m", now=now
    )
    statement = sa.select(DataAlert).where(
        DataAlert.institution_id == 3, DataAlert.scope == "x", DataAlert.status == "open"
    )
    assert session.scalar(statement) is alert
