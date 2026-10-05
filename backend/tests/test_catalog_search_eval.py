"""Unit tests for the search test set and measurement (Task 2.6, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.catalog import search_eval as ev
from app.catalog.search_eval import (
    CANDIDATE_ONLY,
    CORRECT_EMPTY,
    FAILED,
    FALSE_POSITIVE,
    HIT,
    MISS,
    TestsetError,
    entry_matches,
    load_testset,
    render_report,
    rescore_row,
    score_result,
    summarize,
)


def _item(dataset: str, codes: dict | None = None, institution: str = "tuik") -> dict:
    return {
        "institution": institution,
        "dataset": dataset,
        "codes": codes or {},
        "series_external_code": f"{dataset}:x",
        "rating": {"p_high": 0.8},
    }


def _query(group: str = "haber", expected: list | None = None, query_id: str = "q1") -> dict:
    return {
        "id": query_id,
        "group": group,
        "request": "bir istek",
        "expected": [] if expected is None else expected,
    }


# ---- the real test set ------------------------------------------------------


def test_real_testset_loads_and_has_the_agreed_shape() -> None:
    testset = load_testset()
    queries = testset["queries"]
    groups = [query["group"] for query in queries]
    assert testset["threshold"] == 0.70  # user decision 2026-10-05
    assert 30 <= groups.count("haber") + groups.count("agac") <= 40
    assert groups.count("haber") >= 15 and groups.count("agac") >= 15
    assert groups.count("olumsuz") >= 3
    assert len({query["id"] for query in queries}) == len(queries)


# ---- loading ----------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "set.yaml"
    path.write_text(text, encoding="utf-8")
    return path


_OK = """
threshold: 0.7
queries:
  - {id: a, group: haber, request: "x", expected: [{institution: tuik, dataset: D}]}
  - {id: n, group: olumsuz, request: "y", expected: []}
"""


def test_valid_file_loads(tmp_path: Path) -> None:
    assert len(load_testset(_write(tmp_path, _OK))["queries"]) == 2


@pytest.mark.parametrize(
    "text",
    [
        _OK.replace("threshold: 0.7", "threshold: 1.5"),
        _OK.replace("id: n,", "id: a,"),
        _OK.replace("group: haber", "group: baska"),
        _OK.replace("expected: [{institution: tuik, dataset: D}]", "expected: []"),
        _OK.replace(
            'request: "y", expected: []', 'request: "y", expected: [{institution: a, dataset: b}]'
        ),
        _OK.replace("{institution: tuik, dataset: D}", "{institution: tuik}"),
        _OK.replace("dataset: D}", "dataset: D, codes: {A: 1}}"),
        _OK.replace("dataset: D}", "dataset: D, extra: 1}"),
    ],
)
def test_malformed_files_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(TestsetError):
        load_testset(_write(tmp_path, text))


# ---- matching ---------------------------------------------------------------


def test_entry_without_codes_accepts_any_series_of_the_dataset() -> None:
    entry = {"institution": "tuik", "dataset": "D"}
    assert entry_matches(entry, _item("D", {"A": "1"}))
    assert not entry_matches(entry, _item("OTHER"))
    assert not entry_matches(entry, _item("D", institution="tcmb"))


def test_entry_codes_must_all_be_contained() -> None:
    entry = {"institution": "tuik", "dataset": "D", "codes": {"A": "1", "B": "2"}}
    assert entry_matches(entry, _item("D", {"A": "1", "B": "2", "C": "3"}))
    assert not entry_matches(entry, _item("D", {"A": "1"}))
    assert not entry_matches(entry, _item("D", {"A": "1", "B": "9"}))


# ---- scoring ----------------------------------------------------------------


_EXPECTED = [{"institution": "tuik", "dataset": "D"}]


def test_hit_has_a_rank_and_any_acceptable_entry_counts() -> None:
    expected = [*_EXPECTED, {"institution": "tuik", "dataset": "E"}]
    result = {"status": "ok", "strong": [_item("X"), _item("E")], "candidates": []}
    scored = score_result(_query(expected=expected), result)
    assert scored["outcome"] == HIT
    assert scored["rank"] == 2


def test_candidate_only_is_not_a_hit() -> None:
    result = {"status": "no_strong_match", "strong": [], "candidates": [_item("D")]}
    assert score_result(_query(expected=_EXPECTED), result)["outcome"] == CANDIDATE_ONLY


def test_miss() -> None:
    result = {"status": "ok", "strong": [_item("X")], "candidates": [_item("Y")]}
    assert score_result(_query(expected=_EXPECTED), result)["outcome"] == MISS


def test_failed_search_is_never_a_miss_or_a_false_positive() -> None:
    result = {"status": "search_failed", "strong": [], "candidates": []}
    assert score_result(_query(expected=_EXPECTED), result)["outcome"] == FAILED
    assert score_result(_query("olumsuz"), result)["outcome"] == FAILED


def test_negative_query_scores_false_positive_only_on_a_strong_result() -> None:
    empty = {"status": "no_strong_match", "strong": [], "candidates": [_item("D")]}
    assert score_result(_query("olumsuz"), empty)["outcome"] == CORRECT_EMPTY
    strong = {"status": "ok", "strong": [_item("D")], "candidates": []}
    assert score_result(_query("olumsuz"), strong)["outcome"] == FALSE_POSITIVE


# ---- summary ----------------------------------------------------------------


def _row(query_id: str, group: str, outcome: str, rank: int | None = None) -> dict:
    return {"id": query_id, "group": group, "outcome": outcome, "rank": rank}


def test_summary_hit_rate_excludes_failed_and_negative_runs() -> None:
    rows = [
        _row("a", "haber", HIT, 1),
        _row("b", "haber", MISS),
        _row("c", "agac", HIT, 3),
        _row("d", "agac", FAILED),
        _row("n1", "olumsuz", FALSE_POSITIVE),
        _row("n2", "olumsuz", CORRECT_EMPTY),
    ]
    summary = summarize(rows, 0.70)
    assert summary["positive_runs"] == 3
    assert summary["hit_rate"] == pytest.approx(2 / 3)
    assert summary["passes_threshold"] is False
    assert summary["mean_rank_of_hit"] == 2
    assert summary["failed_runs"] == 1
    assert summary["false_positives"] == 1
    assert summary["false_positive_rate"] == 0.5
    assert summary["groups"]["haber"]["hit_rate"] == 0.5


def test_summary_threshold_is_inclusive() -> None:
    rows = [_row(str(i), "haber", HIT if i < 7 else MISS, 1) for i in range(10)]
    assert summarize(rows, 0.70)["passes_threshold"] is True


def test_summary_reports_queries_whose_outcome_flipped_between_runs() -> None:
    rows = [
        _row("a", "haber", HIT, 1),
        _row("a", "haber", MISS),
        _row("b", "haber", HIT, 1),
        _row("b", "haber", HIT, 2),
    ]
    summary = summarize(rows, 0.70)
    assert summary["repeated_queries"] == 2
    assert summary["flipped_queries"] == ["a"]
    assert summary["hit_rate"] == 0.75


def test_summary_with_no_positive_runs_does_not_divide_by_zero() -> None:
    summary = summarize([_row("n", "olumsuz", CORRECT_EMPTY)], 0.70)
    assert summary["hit_rate"] is None
    assert summary["passes_threshold"] is False


def test_report_renders_misses_and_unrun_queries() -> None:
    queries = {
        "a": _query(expected=_EXPECTED, query_id="a"),
        "b": _query(expected=_EXPECTED, query_id="b"),
    }
    rows = [
        {
            **_row("a", "haber", MISS),
            "strong": [{"dataset": "X", "series": "X:1"}],
            "candidates": [],
        }
    ]
    text = render_report(queries, rows, summarize(rows, 0.70))
    assert "EŞİĞİN ALTINDA" in text
    assert "koşulmadı" in text
    assert "### a:" in text


def test_module_exports_are_consistent() -> None:
    for name in ev.__all__:
        assert hasattr(ev, name)


def test_rescore_applies_a_corrected_expected_list_to_a_stored_run() -> None:
    stored = {
        "id": "q1",
        "group": "haber",
        "status": "ok",
        "outcome": MISS,
        "rank": None,
        "strong": [{"institution": "tcmb", "dataset": "NEW", "codes": {}, "series": "NEW:1"}],
        "candidates": [],
    }
    corrected = _query(expected=[{"institution": "tcmb", "dataset": "NEW"}])
    rescored = rescore_row(corrected, stored)
    assert rescored["outcome"] == HIT
    assert rescored["rank"] == 1
    assert rescored["outcome_at_run"] == MISS
    assert rescored["strong"] == stored["strong"]
    assert stored["outcome"] == MISS  # the stored row is not mutated


def test_rescore_leaves_failed_runs_alone() -> None:
    stored = {"id": "q1", "group": "haber", "status": "search_failed", "outcome": FAILED}
    assert rescore_row(_query(expected=_EXPECTED), stored)["outcome"] == FAILED


def test_repeated_queries_do_not_tilt_the_headline() -> None:
    rows = [
        _row("a", "haber", HIT, 1),
        _row("b", "haber", MISS),
        _row("b", "haber", MISS),
        _row("b", "haber", MISS),
        _row("b", "haber", MISS),
    ]
    summary = summarize(rows, 0.70)
    assert summary["hit_rate"] == 0.5  # two queries: 100% and 0%, not 1/5 runs
    assert summary["positive_runs"] == 5
