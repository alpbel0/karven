"""Integration tests for the relation graph repository (Task 3.1).

They run against the isolated ``karven-test`` Neo4j through the shared
integration fixtures (``uv run python scripts/integration.py``). Every test
writes only nodes with a unique per-test prefix and cleans up exactly the nodes
it created; nothing is ever matched-and-wiped globally.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from neo4j import GraphDatabase

from app.config import settings
from app.graph import repository
from app.graph.errors import RelationNotFoundError
from app.graph.models import Hypothesis, PeriodResult, SeriesRef

pytestmark = pytest.mark.integration


def _password() -> str:
    return settings.neo4j_password.get_secret_value() if settings.neo4j_password else ""


@pytest.fixture(scope="module")
def driver():
    neo4j_driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, _password()))
    yield neo4j_driver
    neo4j_driver.close()


class Created:
    """Records the graph nodes a test created so it can delete exactly them."""

    def __init__(self) -> None:
        self.series: set[str] = set()
        self.relations: set[str] = set()
        self.tests: set[str] = set()
        self.news: set[int] = set()

    def cleanup(self, driver) -> None:
        with driver.session() as session:
            if self.tests:
                session.run(
                    "MATCH (t:RelationTest) WHERE t.id IN $ids "
                    "MATCH (p:PeriodResult)-[:RESULT_OF]->(t) DETACH DELETE p",
                    ids=list(self.tests),
                ).consume()
                session.run(
                    "MATCH (t:RelationTest) WHERE t.id IN $ids DETACH DELETE t",
                    ids=list(self.tests),
                ).consume()
            if self.relations:
                session.run(
                    "MATCH (r:Relation) WHERE r.key IN $keys DETACH DELETE r",
                    keys=list(self.relations),
                ).consume()
            if self.series:
                session.run(
                    "MATCH (s:Series) WHERE s.key IN $keys DETACH DELETE s",
                    keys=list(self.series),
                ).consume()
            if self.news:
                session.run(
                    "MATCH (n:News) WHERE n.id IN $ids DETACH DELETE n",
                    ids=list(self.news),
                ).consume()


@pytest.fixture()
def created(driver):
    tracker = Created()
    yield tracker
    tracker.cleanup(driver)


def _prefix() -> str:
    return f"test_{uuid4().hex[:12]}"


def _news_id() -> int:
    return 1_000_000_000 + (uuid4().int % 1_000_000_000)


def _members(created: Created, prefix: str) -> tuple[SeriesRef, tuple[SeriesRef, ...]]:
    target = SeriesRef(prefix, "TARGET")
    drivers = (SeriesRef(prefix, "DRIVER_A"), SeriesRef(prefix, "DRIVER_B"))
    created.series.update(ref.key for ref in (target, *drivers))
    return target, drivers


def _directions(drivers: tuple[SeriesRef, ...], first: str = "positive") -> dict[str, str]:
    """One direction per driver; the first driver gets ``first``, the rest the opposite."""
    other = "negative" if first == "positive" else "positive"
    return {driver.key: (first if index == 0 else other) for index, driver in enumerate(drivers)}


def _results(details: str | None = None) -> tuple[PeriodResult, PeriodResult]:
    return (
        PeriodResult(
            "all_years",
            "supported",
            n_obs=120,
            best_lag=2,
            statistic_name="pearson",
            statistic_value=0.71,
            p_value=0.001,
            start=date(2000, 1, 1),
            end=date(2026, 9, 1),
            transform="annual_pct_change",
            data_range_start=date(2000, 1, 1),
            data_range_end=date(2026, 9, 1),
            details=details,
        ),
        PeriodResult("2017_2026", "insufficient_data"),
    )


def test_acceptance_relation_tests_and_news_round_trip(driver, created) -> None:
    prefix = _prefix()
    target, drivers = _members(created, prefix)
    hypothesis = Hypothesis(
        mechanism="higher driver raises target",
        driver_directions=_directions(drivers),
        lag_min=0,
        lag_max=3,
        transform="annual_pct_change",
        nominal_tl_series=(drivers[0].key,),
    )
    with driver.session() as session:
        relation, was_created = repository.write_relation(session, target, drivers, hypothesis)
    created.relations.add(relation.key)
    assert was_created is True
    assert relation.reliable is None  # fresh hypothesis: no test yet

    news_a, news_b = _news_id(), _news_id()
    created.news.update({news_a, news_b})
    proposal_a = Hypothesis(
        "news a mechanism", _directions(drivers, first="negative"), 1, 2, "difference", ()
    )
    proposal_b = Hypothesis(
        "news b mechanism", _directions(drivers), 0, 0, "period_pct_change", (target.key,)
    )
    with driver.session() as session:
        assert repository.link_news(session, news_a, relation.key, proposal_a) is True
        assert repository.link_news(session, news_b, relation.key, proposal_b) is True

    with driver.session() as session:
        test_one = repository.add_test(
            session,
            relation.key,
            _results(),
            "supported",
            tested_at=datetime(2026, 10, 1, 12, tzinfo=UTC),
        )
        test_two = repository.add_test(
            session,
            relation.key,
            _results(details=json.dumps({"note": "regression"})),
            "periods_differ",
            tested_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
        )
    created.tests.update({test_one.id, test_two.id})

    with driver.session() as session:
        found = repository.find_relation(session, target, drivers)
        got = repository.get_relation(session, relation.key)
        newest = repository.latest_test(session, relation.key)

    assert found is not None and found.key == relation.key
    assert got is not None
    assert got.target == target
    assert set(got.drivers) == set(drivers)
    assert got.hypothesis == hypothesis
    assert got.hypothesis.driver_directions == _directions(drivers)
    assert got.status == "periods_differ"
    assert got.reliable is True
    assert got.created_at is not None

    links = {link.news_id: link for link in got.news_links}
    assert set(links) == {news_a, news_b}
    assert links[news_a].proposal == proposal_a
    assert links[news_b].proposal == proposal_b
    assert links[news_a].proposal.driver_directions == _directions(drivers, first="negative")

    assert [test.id for test in got.tests] == [test_two.id, test_one.id]
    assert got.tests[0].period_results == _results(details=json.dumps({"note": "regression"}))
    assert got.tests[1].period_results == _results()
    assert newest is not None and newest.id == test_two.id


def test_rewriting_same_relation_keeps_first_hypothesis(driver, created) -> None:
    prefix = _prefix()
    target, drivers = _members(created, prefix)
    first = Hypothesis("first mechanism", _directions(drivers), 0, 1, "difference", ())
    with driver.session() as session:
        relation, was_created = repository.write_relation(session, target, drivers, first)
    created.relations.add(relation.key)
    assert was_created is True

    second = Hypothesis(
        "second mechanism", _directions(drivers, first="negative"), 5, 9, "annual_pct_change", ()
    )
    with driver.session() as session:
        again, was_created_again = repository.write_relation(session, target, drivers, second)
    assert was_created_again is False
    assert again.key == relation.key

    with driver.session() as session:
        count = session.run(
            "MATCH (r:Relation {key: $key}) RETURN count(r) AS count", key=relation.key
        ).single()["count"]
        got = repository.get_relation(session, relation.key)
    assert count == 1
    assert got is not None and got.hypothesis == first

    # The second source news keeps its own directions, not the relation hypothesis.
    news = _news_id()
    created.news.add(news)
    proposal = Hypothesis(
        "news proposal", _directions(drivers, first="negative"), 4, 4, "difference", ()
    )
    with driver.session() as session:
        repository.link_news(session, news, relation.key, proposal)
        got = repository.get_relation(session, relation.key)
    assert got is not None
    stored = {link.news_id: link.proposal for link in got.news_links}[news]
    assert stored == proposal
    assert stored.driver_directions == _directions(drivers, first="negative")
    assert stored.driver_directions != first.driver_directions


def test_reverse_direction_is_a_separate_relation(driver, created) -> None:
    prefix = _prefix()
    target_a = SeriesRef(prefix, "A")
    target_b = SeriesRef(prefix, "B")
    created.series.update({target_a.key, target_b.key})
    forward_hypothesis = Hypothesis("m", {target_b.key: "positive"}, 0, 1, "difference", ())
    backward_hypothesis = Hypothesis("m", {target_a.key: "positive"}, 0, 1, "difference", ())

    with driver.session() as session:
        forward, created_forward = repository.write_relation(
            session, target_a, (target_b,), forward_hypothesis
        )
        backward, created_backward = repository.write_relation(
            session, target_b, (target_a,), backward_hypothesis
        )
    created.relations.update({forward.key, backward.key})

    assert created_forward is True and created_backward is True
    assert forward.key != backward.key
    with driver.session() as session:
        count = session.run(
            "MATCH (r:Relation) WHERE r.key IN $keys RETURN count(r) AS count",
            keys=[forward.key, backward.key],
        ).single()["count"]
    assert count == 2


def test_same_news_relation_pair_is_linked_once(driver, created) -> None:
    prefix = _prefix()
    target, drivers = _members(created, prefix)
    hypothesis = Hypothesis("m", _directions(drivers), 0, 1, "difference", ())
    with driver.session() as session:
        relation, _ = repository.write_relation(session, target, drivers, hypothesis)
    created.relations.add(relation.key)

    news = _news_id()
    created.news.add(news)
    proposal = Hypothesis("proposal", _directions(drivers), 0, 1, "difference", ())
    with driver.session() as session:
        first = repository.link_news(session, news, relation.key, proposal)
        second = repository.link_news(session, news, relation.key, proposal)
        count = session.run(
            "MATCH (:News {id: $id})-[l:SOURCE_OF]->(:Relation {key: $key}) "
            "RETURN count(l) AS count",
            id=news,
            key=relation.key,
        ).single()["count"]

    assert first is True and second is False
    assert count == 1


def test_two_tests_keep_the_first_and_set_latest_status(driver, created) -> None:
    prefix = _prefix()
    target, drivers = _members(created, prefix)
    with driver.session() as session:
        relation, _ = repository.write_relation(
            session, target, drivers, Hypothesis("m", _directions(drivers), 0, 1, "difference")
        )
    created.relations.add(relation.key)

    first_results = _results()
    second_results = (
        PeriodResult("all_years", "supported"),
        PeriodResult(
            "2017_2026",
            "unsupported",
            reliable=False,
            reliability_reason="small effective sample",
        ),
    )
    with driver.session() as session:
        first = repository.add_test(
            session,
            relation.key,
            first_results,
            "supported",
            tested_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    created.tests.add(first.id)

    with driver.session() as session:
        after_first = repository.get_relation(session, relation.key)
    assert after_first is not None
    assert after_first.reliable is True

    with driver.session() as session:
        second = repository.add_test(
            session,
            relation.key,
            second_results,
            "unsupported",
            tested_at=datetime(2026, 10, 5, tzinfo=UTC),
        )
    created.tests.add(second.id)

    with driver.session() as session:
        got = repository.get_relation(session, relation.key)
    assert got is not None
    assert got.status == "unsupported"
    # The relation follows the LATEST test: one unreliable period -> False.
    assert got.reliable is False
    by_id = {test.id: test for test in got.tests}
    assert len(by_id) == 2
    assert by_id[first.id].period_results == first_results
    unreliable = next(result for result in by_id[second.id].period_results if not result.reliable)
    assert unreliable.reliability_reason == "small effective sample"


def test_concurrent_writes_create_one_relation(driver, created) -> None:
    prefix = _prefix()
    target, drivers = _members(created, prefix)
    hypothesis = Hypothesis("m", _directions(drivers), 0, 1, "difference", ())

    def worker(_: int) -> tuple[str, bool]:
        with driver.session() as session:
            relation, was_created = repository.write_relation(session, target, drivers, hypothesis)
        return relation.key, was_created

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(8)))

    keys = {key for key, _ in results}
    assert len(keys) == 1
    created.relations.add(next(iter(keys)))
    assert sum(1 for _, was_created in results if was_created) == 1
    with driver.session() as session:
        count = session.run(
            "MATCH (r:Relation {key: $key}) RETURN count(r) AS count", key=next(iter(keys))
        ).single()["count"]
    assert count == 1


def test_three_driver_regression_round_trips_details_and_directions(driver, created) -> None:
    prefix = _prefix()
    target = SeriesRef(prefix, "TARGET")
    drivers = (
        SeriesRef(prefix, "DRIVER_A"),
        SeriesRef(prefix, "DRIVER_B"),
        SeriesRef(prefix, "DRIVER_C"),
    )
    created.series.update(ref.key for ref in (target, *drivers))
    details = json.dumps(
        {
            "coefficients": [
                {"series": drivers[0].key, "lag": 1, "coefficient": 0.42},
                {"series": drivers[1].key, "lag": 2, "coefficient": -0.13},
                {"series": drivers[2].key, "lag": 0, "coefficient": 0.05},
            ]
        }
    )
    # Per-driver directions: at least two differ.
    driver_directions = {
        drivers[0].key: "positive",
        drivers[1].key: "negative",
        drivers[2].key: "positive",
    }
    hypothesis = Hypothesis("m", driver_directions, 0, 2, "difference", ())
    with driver.session() as session:
        relation, _ = repository.write_relation(session, target, drivers, hypothesis)
    created.relations.add(relation.key)
    with driver.session() as session:
        test = repository.add_test(session, relation.key, _results(details=details), "supported")
    created.tests.add(test.id)

    with driver.session() as session:
        got = repository.get_relation(session, relation.key)
        edge_rows = session.run(
            "MATCH (s:Series)-[d:DRIVES]->(r:Relation {key: $key}) "
            "RETURN s.key AS key, d.direction AS direction",
            key=relation.key,
        )
        edge_directions = {row["key"]: row["direction"] for row in edge_rows}
    assert got is not None
    assert set(got.drivers) == set(drivers)
    assert got.hypothesis.driver_directions == driver_directions
    assert got.tests[0].period_results[0].details == details
    assert edge_directions == driver_directions
    assert len(set(edge_directions.values())) >= 2


def test_unknown_relation_raises_and_writes_nothing(driver, created) -> None:
    results = _results()
    with driver.session() as session:
        before = session.run("MATCH (t:RelationTest) RETURN count(t) AS count").single()["count"]

    with pytest.raises(RelationNotFoundError):
        with driver.session() as session:
            repository.add_test(session, uuid4().hex, results, "supported")

    with driver.session() as session:
        after = session.run("MATCH (t:RelationTest) RETURN count(t) AS count").single()["count"]
    assert after == before
