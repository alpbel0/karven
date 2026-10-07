"""Neo4j read/write operations for the relation graph (Task 3.1).

Every write runs as a single transaction through ``session.execute_write`` and
every read through ``session.execute_read``. Cypher values are always passed as
parameters, never interpolated. The functions accept a ``neo4j`` ``Session`` (or
anything with the same ``execute_read``/``execute_write`` shape) so unit tests
can use a fake.

Nothing here decides anything: ``add_test`` receives the relation status as an
argument (Task 3.2 computes it) and a relation's first hypothesis is never
overwritten.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.graph.errors import GraphError, InvalidRelationError, RelationNotFoundError
from app.graph.keys import relation_key
from app.graph.models import (
    PERIODS,
    Hypothesis,
    NewsLink,
    PeriodResult,
    RelationRecord,
    SeriesRef,
    TestRecord,
    normalize_relation,
    validate_driver_directions,
    validate_nominal_members,
    validate_status,
)

_WRITE_RELATION = """
MERGE (r:Relation {key: $key})
ON CREATE SET
    r.mechanism = $mechanism,
    r.driver_directions = $driver_directions,
    r.lag_min = $lag_min,
    r.lag_max = $lag_max,
    r.transform = $transform,
    r.nominal_tl_series = $nominal_tl_series,
    r.created_at = $created_at,
    r.status = 'hypothesis',
    r.`__created` = true
WITH r, coalesce(r.`__created`, false) AS created
REMOVE r.`__created`
WITH r, created
UNWIND $series AS s
MERGE (n:Series {key: s.key})
ON CREATE SET n.institution = s.institution, n.code = s.code
FOREACH (_ IN CASE WHEN s.is_target THEN [1] ELSE [] END | MERGE (r)-[:TARGETS]->(n))
FOREACH (_ IN CASE WHEN s.is_target THEN [] ELSE [1] END |
    MERGE (n)-[d:DRIVES]->(r)
    ON CREATE SET d.direction = s.direction)
WITH DISTINCT r, created
RETURN r AS relation, created AS created
"""

_LINK_NEWS = """
MATCH (r:Relation {key: $relation_key})
MERGE (n:News {id: $news_id})
ON CREATE SET n.created_at = $linked_at
MERGE (n)-[l:SOURCE_OF]->(r)
ON CREATE SET
    l.mechanism = $mechanism,
    l.driver_directions = $driver_directions,
    l.lag_min = $lag_min,
    l.lag_max = $lag_max,
    l.transform = $transform,
    l.nominal_tl_series = $nominal_tl_series,
    l.linked_at = $linked_at,
    l.`__created` = true
WITH l, coalesce(l.`__created`, false) AS created
REMOVE l.`__created`
WITH created
RETURN created AS created
"""

_ADD_TEST = """
MATCH (r:Relation {key: $relation_key})
SET r.status = $status, r.reliable = $reliable
CREATE (t:RelationTest {id: $test_id, tested_at: $tested_at})
CREATE (t)-[:TESTS]->(r)
WITH t
UNWIND $periods AS p
CREATE (pr:PeriodResult)
SET pr = p
CREATE (pr)-[:RESULT_OF]->(t)
WITH t, count(pr) AS period_count
RETURN t.id AS id, t.tested_at AS tested_at, period_count AS period_count
"""

_GET_RELATION = """
MATCH (r:Relation {key: $key})
OPTIONAL MATCH (r)-[:TARGETS]->(target:Series)
OPTIONAL MATCH (driver:Series)-[:DRIVES]->(r)
RETURN r AS relation,
       collect(DISTINCT target) AS targets,
       collect(DISTINCT driver) AS drivers
"""

_GET_NEWS_LINKS = """
MATCH (r:Relation {key: $key})<-[l:SOURCE_OF]-(n:News)
RETURN n.id AS news_id,
       l.mechanism AS mechanism,
       l.driver_directions AS driver_directions,
       l.lag_min AS lag_min,
       l.lag_max AS lag_max,
       l.transform AS transform,
       l.nominal_tl_series AS nominal_tl_series,
       l.linked_at AS linked_at
ORDER BY n.id
"""

_READ_RELATION_DIRECTIONS = """
MATCH (r:Relation {key: $relation_key})
RETURN r.driver_directions AS driver_directions
"""

_GET_TESTS = """
MATCH (r:Relation {key: $key})<-[:TESTS]-(t:RelationTest)
OPTIONAL MATCH (t)<-[:RESULT_OF]-(p:PeriodResult)
RETURN t.id AS id,
       t.tested_at AS tested_at,
       properties(p) AS period_props
ORDER BY t.tested_at DESC, t.id
"""


def write_relation(
    session: Any, target: SeriesRef, drivers: Any, hypothesis: Hypothesis
) -> tuple[RelationRecord, bool]:
    """MERGE a relation by identity and return it with a ``created`` flag.

    Writing an existing relation is a no-op for its stored hypothesis: the first
    hypothesis is never overwritten.
    """
    target, ordered_drivers = normalize_relation(target, drivers)
    if not isinstance(hypothesis, Hypothesis):
        raise InvalidRelationError("hypothesis must be a Hypothesis")
    validate_driver_directions(hypothesis, ordered_drivers)
    validate_nominal_members(hypothesis, target, ordered_drivers)
    key = relation_key(target.key, [driver.key for driver in ordered_drivers])
    series = [
        {
            "key": target.key,
            "institution": target.institution,
            "code": target.code,
            "is_target": True,
            "direction": None,
        }
    ]
    series.extend(
        {
            "key": driver.key,
            "institution": driver.institution,
            "code": driver.code,
            "is_target": False,
            "direction": hypothesis.driver_directions[driver.key],
        }
        for driver in ordered_drivers
    )
    return session.execute_write(
        _write_relation_tx,
        key=key,
        series=series,
        hypothesis=hypothesis,
        target=target,
        drivers=ordered_drivers,
    )


def _write_relation_tx(
    tx: Any,
    *,
    key: str,
    series: list[dict[str, Any]],
    hypothesis: Hypothesis,
    target: SeriesRef,
    drivers: tuple[SeriesRef, ...],
) -> tuple[RelationRecord, bool]:
    params = {
        "key": key,
        "series": series,
        "created_at": datetime.now(UTC),
        **_hypothesis_params(hypothesis),
    }
    record = tx.run(_WRITE_RELATION, **params).single()
    if record is None:
        raise GraphError("write_relation returned no relation")
    node = record["relation"]
    relation = RelationRecord(
        key=key,
        target=target,
        drivers=drivers,
        hypothesis=_hypothesis_from_node(node),
        status=node.get("status") or "hypothesis",
        reliable=node.get("reliable"),
        created_at=node.get("created_at"),
    )
    return relation, bool(record["created"])


def link_news(session: Any, news_id: int, relation_key: str, proposal: Hypothesis) -> bool:
    """MERGE the ``(News)-[:SOURCE_OF]->(Relation)`` link; return True if new.

    The link carries the news' own proposal. Linking the same pair twice is a
    no-op (returns False) and never changes the first proposal.
    """
    if not isinstance(news_id, int) or isinstance(news_id, bool):
        raise InvalidRelationError("news_id must be an integer")
    if not isinstance(proposal, Hypothesis):
        raise InvalidRelationError("proposal must be a Hypothesis")
    created = session.execute_write(
        _link_news_tx,
        news_id=news_id,
        relation_key=relation_key,
        proposal=proposal,
    )
    if created is None:
        raise RelationNotFoundError(f"relation {relation_key!r} does not exist")
    return bool(created)


def _link_news_tx(tx: Any, *, news_id: int, relation_key: str, proposal: Hypothesis) -> bool | None:
    row = tx.run(_READ_RELATION_DIRECTIONS, relation_key=relation_key).single()
    if row is None:
        return None
    existing = _parse_driver_directions(row["driver_directions"])
    if set(existing) != set(proposal.driver_directions):
        raise InvalidRelationError(
            "a news proposal's driver_directions must cover the same driver series as the "
            f"relation (relation={sorted(existing)}, proposal={sorted(proposal.driver_directions)})"
        )
    record = tx.run(
        _LINK_NEWS,
        news_id=news_id,
        relation_key=relation_key,
        linked_at=datetime.now(UTC),
        **_hypothesis_params(proposal),
    ).single()
    return None if record is None else bool(record["created"])


def add_test(
    session: Any,
    relation_key: str,
    period_results: Any,
    status: str,
    *,
    tested_at: datetime | None = None,
) -> TestRecord:
    """Append an immutable test and set the relation status in one transaction.

    Requires exactly the two periods (``all_years`` and ``2017_2026``). The
    status is taken as given: Task 3.2 computes it.
    """
    if not isinstance(relation_key, str) or not relation_key:
        raise InvalidRelationError("relation_key must be a non-empty string")
    validate_status(status)
    results = _normalize_period_results(period_results)
    if tested_at is not None and not isinstance(tested_at, datetime):
        raise InvalidRelationError("tested_at must be a datetime or None")
    test_id = uuid4().hex
    # The relation is reliable only when both period results of this test are;
    # derived here, never passed in by the caller.
    reliable = all(result.reliable for result in results)
    record = session.execute_write(
        _add_test_tx,
        relation_key=relation_key,
        test_id=test_id,
        status=status,
        reliable=reliable,
        tested_at=tested_at or datetime.now(UTC),
        periods=[_period_params(result) for result in results],
    )
    if record is None:
        raise RelationNotFoundError(f"relation {relation_key!r} does not exist")
    return TestRecord(
        id=record["id"],
        relation_key=relation_key,
        tested_at=record["tested_at"],
        period_results=results,
    )


def _add_test_tx(
    tx: Any,
    *,
    relation_key: str,
    test_id: str,
    status: str,
    reliable: bool,
    tested_at: datetime,
    periods: list[dict[str, Any]],
) -> dict[str, Any] | None:
    record = tx.run(
        _ADD_TEST,
        relation_key=relation_key,
        test_id=test_id,
        status=status,
        reliable=reliable,
        tested_at=tested_at,
        periods=periods,
    ).single()
    if record is None:
        return None
    return {"id": record["id"], "tested_at": record["tested_at"]}


def find_relation(session: Any, target: SeriesRef, drivers: Any) -> RelationRecord | None:
    """Return the relation with this identity (target + drivers), or None."""
    target, ordered_drivers = normalize_relation(target, drivers)
    key = relation_key(target.key, [driver.key for driver in ordered_drivers])
    return get_relation(session, key)


def get_relation(session: Any, relation_key: str) -> RelationRecord | None:
    """Read a relation with members, hypothesis, status, news links and tests."""
    if not isinstance(relation_key, str) or not relation_key:
        raise InvalidRelationError("relation_key must be a non-empty string")
    return session.execute_read(_get_relation_tx, key=relation_key)


def _get_relation_tx(tx: Any, *, key: str) -> RelationRecord | None:
    row = tx.run(_GET_RELATION, key=key).single()
    if row is None:
        return None
    node = row["relation"]
    targets = [series for series in row["targets"] if series is not None]
    drivers = [series for series in row["drivers"] if series is not None]
    if not targets:
        raise GraphError(f"relation {key!r} has no target series")
    return RelationRecord(
        key=key,
        target=_series_from_node(targets[0]),
        drivers=tuple(_series_from_node(series) for series in sorted(drivers, key=_node_key)),
        hypothesis=_hypothesis_from_node(node),
        status=node.get("status") or "hypothesis",
        reliable=node.get("reliable"),
        created_at=node.get("created_at"),
        news_links=_read_news_links(tx, key),
        tests=_read_tests(tx, key),
    )


def latest_test(session: Any, relation_key: str) -> TestRecord | None:
    """Return the newest test (by ``tested_at``) for a relation, or None."""
    if not isinstance(relation_key, str) or not relation_key:
        raise InvalidRelationError("relation_key must be a non-empty string")
    return session.execute_read(_latest_test_tx, key=relation_key)


def _latest_test_tx(tx: Any, *, key: str) -> TestRecord | None:
    tests = _read_tests(tx, key)
    return tests[0] if tests else None


def _read_news_links(tx: Any, key: str) -> tuple[NewsLink, ...]:
    links: list[NewsLink] = []
    for row in tx.run(_GET_NEWS_LINKS, key=key):
        links.append(
            NewsLink(
                news_id=row["news_id"],
                proposal=_hypothesis_from_values(
                    mechanism=row["mechanism"],
                    driver_directions=row["driver_directions"],
                    lag_min=row["lag_min"],
                    lag_max=row["lag_max"],
                    transform=row["transform"],
                    nominal_tl_series=row["nominal_tl_series"],
                ),
                linked_at=row["linked_at"],
            )
        )
    return tuple(links)


def _read_tests(tx: Any, key: str) -> tuple[TestRecord, ...]:
    # Group the flat (test, period-map) rows while preserving the tested_at DESC
    # order. ``properties(p)`` yields a map so absent/null fields are simply
    # missing keys; Neo4j never stores null properties.
    order: list[str] = []
    tested_at: dict[str, datetime] = {}
    periods: dict[str, list[PeriodResult]] = {}
    for row in tx.run(_GET_TESTS, key=key):
        test_id = row["id"]
        if test_id not in periods:
            order.append(test_id)
            tested_at[test_id] = row["tested_at"]
            periods[test_id] = []
        props = row["period_props"]
        if props is None:
            # OPTIONAL MATCH with no period node for this test.
            continue
        periods[test_id].append(
            PeriodResult(
                period=props.get("period"),
                result=props.get("result"),
                n_obs=props.get("n_obs"),
                best_lag=props.get("best_lag"),
                statistic_name=props.get("statistic_name"),
                statistic_value=props.get("statistic_value"),
                p_value=props.get("p_value"),
                start=props.get("start"),
                end=props.get("end"),
                transform=props.get("transform"),
                data_range_start=props.get("data_range_start"),
                data_range_end=props.get("data_range_end"),
                details=props.get("details"),
                reliable=props.get("reliable", True),
                reliability_reason=props.get("reliability_reason"),
            )
        )
    return tuple(
        TestRecord(
            id=test_id,
            relation_key=key,
            tested_at=tested_at[test_id],
            period_results=tuple(
                sorted(periods[test_id], key=lambda item: PERIODS.index(item.period))
            ),
        )
        for test_id in order
    )


def _normalize_period_results(period_results: Any) -> tuple[PeriodResult, ...]:
    try:
        results = tuple(period_results)
    except TypeError as exc:
        raise InvalidRelationError("period_results must be a collection of PeriodResult") from exc
    if len(results) != 2:
        raise InvalidRelationError(
            "a test requires exactly the two periods (all_years and 2017_2026)"
        )
    if any(not isinstance(result, PeriodResult) for result in results):
        raise InvalidRelationError("every period result must be a PeriodResult")
    if {result.period for result in results} != {"all_years", "2017_2026"}:
        raise InvalidRelationError(
            "a test requires exactly the two periods (all_years and 2017_2026)"
        )
    return results


def _period_params(result: PeriodResult) -> dict[str, Any]:
    return {
        "period": result.period,
        "result": result.result,
        "n_obs": result.n_obs,
        "best_lag": result.best_lag,
        "statistic_name": result.statistic_name,
        "statistic_value": result.statistic_value,
        "p_value": result.p_value,
        "start": result.start,
        "end": result.end,
        "transform": result.transform,
        "data_range_start": result.data_range_start,
        "data_range_end": result.data_range_end,
        "details": result.details,
        "reliable": result.reliable,
        "reliability_reason": result.reliability_reason,
    }


def _hypothesis_params(hypothesis: Hypothesis) -> dict[str, Any]:
    return {
        "mechanism": hypothesis.mechanism,
        # Neo4j properties cannot hold maps, so the per-driver directions are
        # stored as a JSON string with sorted keys (deterministic).
        "driver_directions": _dump_driver_directions(hypothesis.driver_directions),
        "lag_min": hypothesis.lag_min,
        "lag_max": hypothesis.lag_max,
        "transform": hypothesis.transform,
        "nominal_tl_series": list(hypothesis.nominal_tl_series),
    }


def _hypothesis_from_node(node: Any) -> Hypothesis:
    return _hypothesis_from_values(
        mechanism=node.get("mechanism"),
        driver_directions=node.get("driver_directions"),
        lag_min=node.get("lag_min"),
        lag_max=node.get("lag_max"),
        transform=node.get("transform"),
        nominal_tl_series=node.get("nominal_tl_series"),
    )


def _hypothesis_from_values(
    *,
    mechanism: Any,
    driver_directions: Any,
    lag_min: Any,
    lag_max: Any,
    transform: Any,
    nominal_tl_series: Any,
) -> Hypothesis:
    return Hypothesis(
        mechanism=mechanism,
        driver_directions=_parse_driver_directions(driver_directions),
        lag_min=lag_min,
        lag_max=lag_max,
        transform=transform,
        nominal_tl_series=tuple(nominal_tl_series or ()),
    )


def _dump_driver_directions(directions: Any) -> str:
    return json.dumps(dict(sorted(directions.items())), ensure_ascii=False, separators=(",", ":"))


def _parse_driver_directions(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GraphError(f"invalid driver_directions JSON: {value!r}") from exc
    elif isinstance(value, Mapping):
        parsed = dict(value)
    else:
        raise GraphError(f"driver_directions must be a JSON string or mapping, got {type(value)}")
    if not isinstance(parsed, dict):
        raise GraphError("driver_directions JSON must be an object")
    return parsed


def _series_from_node(node: Any) -> SeriesRef:
    return SeriesRef(institution=node["institution"], code=node["code"])


def _node_key(node: Any) -> str:
    return node["key"]
