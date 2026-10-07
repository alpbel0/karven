"""Unit tests for the graph repository input validation and plumbing.

The validation tests must raise before any Cypher runs, so the fake session
records calls and asserts none happened. The plumbing tests use a fake
transaction returning canned records.

A fake session can never catch a Cypher *syntax* error (it never parses the
query), so a cheap regex guard below checks clause order instead.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from app.graph import repository
from app.graph.errors import InvalidRelationError, RelationNotFoundError
from app.graph.models import Hypothesis, PeriodResult, SeriesRef

TARGET = SeriesRef("T", "TARGET")
DRIVER = SeriesRef("T", "DRIVER")
HYPOTHESIS = Hypothesis("mechanism", {DRIVER.key: "positive"}, 0, 1, "difference")


class RecordingSession:
    def __init__(self) -> None:
        self.write_calls: list[Any] = []
        self.read_calls: list[Any] = []

    def execute_write(self, *args: Any, **kwargs: Any) -> Any:
        self.write_calls.append((args, kwargs))
        raise AssertionError("execute_write must not run for invalid input")

    def execute_read(self, *args: Any, **kwargs: Any) -> Any:
        self.read_calls.append((args, kwargs))
        raise AssertionError("execute_read must not run for invalid input")


def _two_periods() -> tuple[PeriodResult, PeriodResult]:
    return (
        PeriodResult("all_years", "supported"),
        PeriodResult("2017_2026", "unsupported"),
    )


def test_write_relation_rejects_driver_directions_not_matching_drivers() -> None:
    session = RecordingSession()
    hypothesis = Hypothesis("m", {"other|key": "positive"}, 0, 1, "difference")

    with pytest.raises(InvalidRelationError):
        repository.write_relation(session, TARGET, [DRIVER], hypothesis)

    assert session.write_calls == []


def test_write_relation_rejects_nominal_key_not_a_member() -> None:
    session = RecordingSession()
    hypothesis = Hypothesis("m", {DRIVER.key: "positive"}, 0, 1, "difference", ("nope|key",))

    with pytest.raises(InvalidRelationError):
        repository.write_relation(session, TARGET, [DRIVER], hypothesis)

    assert session.write_calls == []


def test_write_relation_rejects_bad_hypothesis_type() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.write_relation(session, TARGET, [DRIVER], {"mechanism": "m"})  # type: ignore[arg-type]

    assert session.write_calls == []


def test_write_relation_rejects_bad_members() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.write_relation(session, TARGET, [], HYPOTHESIS)

    assert session.write_calls == []


def test_add_test_rejects_bad_status() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.add_test(session, "key", _two_periods(), "rejected")

    assert session.write_calls == []


def test_add_test_requires_exactly_two_periods() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.add_test(session, "key", (PeriodResult("all_years", "supported"),), "supported")
    with pytest.raises(InvalidRelationError):
        repository.add_test(
            session,
            "key",
            (
                PeriodResult("all_years", "supported"),
                PeriodResult("all_years", "unsupported"),
            ),
            "supported",
        )

    assert session.write_calls == []


def test_add_test_rejects_bad_tested_at() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.add_test(session, "key", _two_periods(), "supported", tested_at="now")  # type: ignore[arg-type]

    assert session.write_calls == []


def test_link_news_rejects_bad_news_id() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.link_news(session, "1", "key", HYPOTHESIS)  # type: ignore[arg-type]

    assert session.write_calls == []


def test_get_relation_rejects_blank_key() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.get_relation(session, "")

    assert session.read_calls == []


def test_latest_test_rejects_blank_key() -> None:
    session = RecordingSession()

    with pytest.raises(InvalidRelationError):
        repository.latest_test(session, "")

    assert session.read_calls == []


class FakeNode(dict):
    pass


class FakeResult:
    def __init__(self, record: dict[str, Any] | None) -> None:
        self._record = record

    def single(self) -> dict[str, Any] | None:
        return self._record


class FakeTx:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.queries: list[str] = []

    def run(self, query: str, **params: Any) -> FakeResult:
        self.queries.append(query)
        return FakeResult(self._handler(query, params))


class FakeSession:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.write_calls = 0
        self.read_calls = 0

    def execute_write(self, fn: Any, **kwargs: Any) -> Any:
        self.write_calls += 1
        return fn(FakeTx(self._handler), **kwargs)

    def execute_read(self, fn: Any, **kwargs: Any) -> Any:
        self.read_calls += 1
        return fn(FakeTx(self._handler), **kwargs)


def _relation_node() -> FakeNode:
    return FakeNode(
        mechanism="mechanism",
        driver_directions=json.dumps({DRIVER.key: "positive"}),
        lag_min=0,
        lag_max=1,
        transform="difference",
        nominal_tl_series=[],
        status="hypothesis",
        created_at=None,
    )


def test_write_relation_runs_one_transaction_and_returns_record() -> None:
    session = FakeSession(lambda query, params: {"relation": _relation_node(), "created": True})

    relation, created = repository.write_relation(session, TARGET, [DRIVER], HYPOTHESIS)

    assert created is True
    assert session.write_calls == 1
    assert relation.key == repository.relation_key(TARGET.key, [DRIVER.key])
    assert relation.hypothesis == HYPOTHESIS
    # A fresh hypothesis has no reliability yet.
    assert relation.reliable is None


def test_link_news_returns_false_when_already_linked() -> None:
    def handler(query: str, params: dict[str, Any]) -> dict[str, Any]:
        if "AS driver_directions" in query:
            return {"driver_directions": json.dumps({DRIVER.key: "positive"})}
        return {"created": False}

    session = FakeSession(handler)

    assert repository.link_news(session, 7, "key", HYPOTHESIS) is False
    assert session.write_calls == 1


def test_link_news_rejects_proposal_with_different_driver_keys() -> None:
    def handler(query: str, params: dict[str, Any]) -> dict[str, Any]:
        if "AS driver_directions" in query:
            return {"driver_directions": json.dumps({"other|key": "positive"})}
        return {"created": True}

    session = FakeSession(handler)

    with pytest.raises(InvalidRelationError):
        repository.link_news(session, 7, "key", HYPOTHESIS)


def test_add_test_returns_test_record() -> None:
    tested_at = datetime(2026, 10, 5, tzinfo=UTC)
    session = FakeSession(lambda query, params: {"id": "test-id", "tested_at": tested_at})

    test = repository.add_test(session, "key", _two_periods(), "supported", tested_at=tested_at)

    assert test.id == "test-id"
    assert test.relation_key == "key"
    assert test.tested_at == tested_at
    assert test.period_results == _two_periods()
    assert session.write_calls == 1


def test_add_test_unknown_relation_raises() -> None:
    session = FakeSession(lambda query, params: None)

    with pytest.raises(RelationNotFoundError):
        repository.add_test(session, "missing", _two_periods(), "supported")


def test_link_news_unknown_relation_raises() -> None:
    session = FakeSession(lambda query, params: None)

    with pytest.raises(RelationNotFoundError):
        repository.link_news(session, 7, "missing", HYPOTHESIS)


def test_add_test_derives_relation_reliability_from_periods() -> None:
    captured: dict[str, Any] = {}

    def handler(query: str, params: dict[str, Any]) -> dict[str, Any]:
        captured.update(params)
        return {"id": "test-id", "tested_at": datetime(2026, 10, 5, tzinfo=UTC)}

    unreliable = (
        PeriodResult("all_years", "supported"),
        PeriodResult(
            "2017_2026", "unsupported", reliable=False, reliability_reason="small effective sample"
        ),
    )
    repository.add_test(FakeSession(handler), "key", unreliable, "unsupported")
    assert captured["reliable"] is False

    captured.clear()
    repository.add_test(FakeSession(handler), "key", _two_periods(), "supported")
    assert captured["reliable"] is True


# A REMOVE must be separated from a following reading clause (MATCH/UNWIND) by a
# WITH, or Neo4j 5 rejects the query. A fake session cannot catch this: only real
# Neo4j parses the Cypher, so this cheap regex guard stands in for the parser.
_REMOVE_BEFORE_READ = re.compile(
    r"\bREMOVE\b(?:(?!\bWITH\b).)*?\b(?:UNWIND|MATCH)\b",
    re.IGNORECASE | re.DOTALL,
)


def test_cypher_remove_is_separated_from_reading_clauses() -> None:
    constants = {
        name: value
        for name, value in vars(repository).items()
        if name.isupper() and isinstance(value, str)
    }

    assert constants, "expected Cypher constants in repository"
    for name, query in constants.items():
        assert not _REMOVE_BEFORE_READ.search(query), (
            f"{name} needs a WITH between REMOVE and a reading clause"
        )


#: PeriodResult fields that may be absent because Neo4j does not store null
#: property values (only ``period``/``result`` are always set). The query must
#: not access any of them as a property; the map is read in Python instead.
_DOTTED_PROPERTY_AS = re.compile(r"p\.`?([a-z_]+)`?\s+AS", re.IGNORECASE)
_SUBSCRIPT_PROPERTY_AS = re.compile(r"p\[\s*['\"]([a-z_]+)['\"]\s*\]\s+AS", re.IGNORECASE)


def test_get_tests_returns_period_results_as_a_map() -> None:
    # Neo4j does not store null properties; both dotted and literal-subscript
    # access to a property key that no node has emits a DBMS notification. The
    # query must therefore return ``properties(p)`` as one map and read every
    # field (nullable or not) from it in Python with ``.get(...)``.
    query = repository._GET_TESTS
    dotted = {match.group(1) for match in _DOTTED_PROPERTY_AS.finditer(query)}
    subscript = {match.group(1) for match in _SUBSCRIPT_PROPERTY_AS.finditer(query)}

    assert dotted == set(), dotted
    assert subscript == set(), subscript
    assert "properties(p) AS period_props" in query
