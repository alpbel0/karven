"""Search test set and measurement for ``find_series`` (Task 2.6, DECISIONS §7).

The test set (``search_testset.yaml``) maps plain-language requests to the
ACCEPTABLE series (user decision 2026-10-05). A positive query is a *hit* when a
``strong`` result matches any acceptable entry; a result that only appears among
the weak ``candidates`` is reported separately and does not count. A negative
query (data the catalog does not hold) is a *false positive* when anything comes
back ``strong``.

Commands (from ``backend/``)::

    python -m app.catalog.search_eval validate
    python -m app.catalog.search_eval run [--only h01 h02] [--repeat 2] [--out DIR]
    python -m app.catalog.search_eval report DIR

``run`` appends one JSON line per search to ``DIR/results.jsonl`` as it goes (a
crash loses nothing; ``--resume`` skips what is already there) and writes
``DIR/report.md``. The report is rebuilt from the JSONL by ``report``.

The measurement reads the real catalog and calls the real Jev/EVREN services, so it
is a live measurement (about 20 seconds per search), not part of the unit suite.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy.orm import Session

from app.catalog.search import (
    ensure_default_prompts,
    load_search_prompts,
    search_series,
)
from app.data.models import Dataset, DatasetDimension, DimensionCode, Institution

logger = logging.getLogger(__name__)

TESTSET_PATH = Path(__file__).with_name("search_testset.yaml")
DEFAULT_OUT_ROOT = Path("search-eval")  # relative to the working directory

GROUPS = ("haber", "agac", "olumsuz")
POSITIVE_GROUPS = ("haber", "agac")
NEGATIVE_GROUP = "olumsuz"

HIT = "hit"  # an acceptable series is among the strong results
CANDIDATE_ONLY = "candidate_only"  # only among the weak candidates
MISS = "miss"
FAILED = "failed"  # search_failed: counted separately, never as a miss
CORRECT_EMPTY = "correct_empty"  # negative query, nothing strong came back
FALSE_POSITIVE = "false_positive"  # negative query, something strong came back


class TestsetError(ValueError):
    """The test set file is malformed."""

    __test__ = False  # not a pytest class


# --------------------------------------------------------------------------- #
# Test set
# --------------------------------------------------------------------------- #


def load_testset(path: Path = TESTSET_PATH) -> dict[str, Any]:
    """Load and validate the test set (shape only; ``validate`` checks the catalog)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise TestsetError("test set must be a mapping")
    threshold = data.get("threshold")
    if not isinstance(threshold, int | float) or not 0 < threshold <= 1:
        raise TestsetError("threshold must be a number in (0, 1]")
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise TestsetError("queries must be a non-empty list")
    seen: set[str] = set()
    for query in queries:
        if not isinstance(query, Mapping):
            raise TestsetError(f"query must be a mapping: {query!r}")
        query_id = query.get("id")
        if not isinstance(query_id, str) or not query_id:
            raise TestsetError(f"query needs an id: {query!r}")
        if query_id in seen:
            raise TestsetError(f"duplicate query id {query_id!r}")
        seen.add(query_id)
        if query.get("group") not in GROUPS:
            raise TestsetError(f"{query_id}: group must be one of {GROUPS}")
        request = query.get("request")
        if not isinstance(request, str) or not request.strip():
            raise TestsetError(f"{query_id}: request must be non-empty text")
        expected = query.get("expected")
        if not isinstance(expected, list):
            raise TestsetError(f"{query_id}: expected must be a list")
        if query["group"] == NEGATIVE_GROUP and expected:
            raise TestsetError(f"{query_id}: a negative query must have no expected series")
        if query["group"] != NEGATIVE_GROUP and not expected:
            raise TestsetError(f"{query_id}: a positive query needs at least one expected series")
        for entry in expected:
            _check_entry(query_id, entry)
    return dict(data)


def _check_entry(query_id: str, entry: Any) -> None:
    if not isinstance(entry, Mapping):
        raise TestsetError(f"{query_id}: expected entries must be mappings")
    for key in ("institution", "dataset"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise TestsetError(f"{query_id}: expected entry needs {key}")
    codes = entry.get("codes", {})
    if not isinstance(codes, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in codes.items()
    ):
        raise TestsetError(f"{query_id}: codes must map string dimension -> string code")
    extra = set(entry) - {"institution", "dataset", "codes"}
    if extra:
        raise TestsetError(f"{query_id}: unknown expected keys {sorted(extra)}")


# --------------------------------------------------------------------------- #
# Matching and scoring (pure)
# --------------------------------------------------------------------------- #


def entry_matches(entry: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Whether a search result item satisfies an expected entry."""
    if item.get("institution") != entry["institution"] or item.get("dataset") != entry["dataset"]:
        return False
    item_codes = item.get("codes") or {}
    return all(item_codes.get(dim) == code for dim, code in (entry.get("codes") or {}).items())


def _first_match(
    expected: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]]
) -> tuple[int, Mapping[str, Any]] | None:
    """Rank (1-based) and expected entry of the first item that matches any entry."""
    for rank, item in enumerate(items, start=1):
        for entry in expected:
            if entry_matches(entry, item):
                return rank, entry
    return None


def score_result(query: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """Score one search result against its query (no I/O)."""
    strong = list(result.get("strong") or [])
    candidates = list(result.get("candidates") or [])
    status = result.get("status")
    scored: dict[str, Any] = {
        "id": query["id"],
        "group": query["group"],
        "status": status,
        "strong_count": len(strong),
        "candidate_count": len(candidates),
        "strong": [_brief(item) for item in strong],
        "candidates": [_brief(item) for item in candidates],
        "rank": None,
    }
    if status == "search_failed":
        scored["outcome"] = FAILED
        return scored
    if query["group"] == NEGATIVE_GROUP:
        scored["outcome"] = FALSE_POSITIVE if strong else CORRECT_EMPTY
        return scored
    expected = query.get("expected") or []
    found = _first_match(expected, strong)
    if found is not None:
        scored["outcome"] = HIT
        scored["rank"] = found[0]
        return scored
    scored["outcome"] = CANDIDATE_ONLY if _first_match(expected, candidates) else MISS
    return scored


def rescore_row(query: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """Re-score a stored run against the CURRENT test set (no search is repeated).

    A stored row keeps the strong results and candidates it saw, so a corrected
    expected list can be applied to old runs. The outcome at run time stays in
    ``outcome_at_run`` so the report can show what the first scoring said.
    """
    if row["outcome"] == FAILED:
        return dict(row)
    rescored = score_result(
        query,
        {"status": row["status"], "strong": row["strong"], "candidates": row["candidates"]},
    )
    merged = dict(row)
    merged["outcome_at_run"] = row.get("outcome_at_run", row["outcome"])
    merged["outcome"] = rescored["outcome"]
    merged["rank"] = rescored["rank"]
    return merged


def _brief(item: Mapping[str, Any]) -> dict[str, Any]:
    rating = item.get("rating") or {}
    return {
        "institution": item.get("institution"),
        "dataset": item.get("dataset"),
        "codes": item.get("codes"),
        "series": item.get("series_external_code"),
        "p_high": rating.get("p_high"),
        "uyumsuzluk": item.get("uyumsuzluk") or [],
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def summarize(scored: Iterable[Mapping[str, Any]], threshold: float) -> dict[str, Any]:
    """Aggregate scored runs into the headline numbers.

    With repeated runs of a query, each *run* counts as one observation for the
    hit rate (the mean over runs), and ``consistency`` reports queries whose
    outcome flipped between runs.
    """
    rows = list(scored)
    by_query: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_query[row["id"]].append(row)

    def positive(row: Mapping[str, Any]) -> bool:
        return row["group"] in POSITIVE_GROUPS

    positives = [row for row in rows if positive(row) and row["outcome"] != FAILED]
    hits = [row for row in positives if row["outcome"] == HIT]
    negatives = [row for row in rows if row["group"] == NEGATIVE_GROUP and row["outcome"] != FAILED]
    false_positives = [row for row in negatives if row["outcome"] == FALSE_POSITIVE]
    # Query-weighted: a query that was repeated counts once (the mean of its runs),
    # so re-running a few borderline queries for variance never tilts the headline.
    per_query: list[float] = []
    for runs in by_query.values():
        scored_runs = [run for run in runs if positive(run) and run["outcome"] != FAILED]
        if scored_runs:
            per_query.append(
                sum(1 for run in scored_runs if run["outcome"] == HIT) / len(scored_runs)
            )
    hit_rate = statistics.fmean(per_query) if per_query else None

    groups: dict[str, dict[str, Any]] = {}
    for group in POSITIVE_GROUPS:
        group_rows = [row for row in positives if row["group"] == group]
        groups[group] = {
            "runs": len(group_rows),
            "hits": sum(1 for row in group_rows if row["outcome"] == HIT),
            "hit_rate": _rate(
                sum(1 for row in group_rows if row["outcome"] == HIT), len(group_rows)
            ),
        }

    ranks = [row["rank"] for row in hits if row["rank"] is not None]
    outcome_counts = Counter(row["outcome"] for row in rows)

    flipped = sorted(
        query_id for query_id, runs in by_query.items() if len({run["outcome"] for run in runs}) > 1
    )
    repeated = sum(1 for runs in by_query.values() if len(runs) > 1)

    return {
        "threshold": threshold,
        "queries": len(by_query),
        "runs": len(rows),
        "positive_runs": len(positives),
        "hits": len(hits),
        "hit_rate": hit_rate,
        "passes_threshold": hit_rate is not None and hit_rate >= threshold,
        "outcomes": dict(outcome_counts),
        "groups": groups,
        "mean_rank_of_hit": statistics.fmean(ranks) if ranks else None,
        "negative_runs": len(negatives),
        "false_positives": len(false_positives),
        "false_positive_rate": _rate(len(false_positives), len(negatives)),
        "failed_runs": outcome_counts.get(FAILED, 0),
        "repeated_queries": repeated,
        "flipped_queries": flipped,
    }


# --------------------------------------------------------------------------- #
# Catalog validation
# --------------------------------------------------------------------------- #


def validate_against_catalog(session: Session, testset: Mapping[str, Any]) -> list[str]:
    """Problems found when checking every expected entry against the live catalog."""
    problems: list[str] = []
    for query in testset["queries"]:
        for entry in query["expected"]:
            dataset = session.scalars(
                sa.select(Dataset)
                .join(Institution, Dataset.institution_id == Institution.id)
                .where(
                    Institution.code == entry["institution"],
                    Dataset.external_code == entry["dataset"],
                )
            ).first()
            where = f"{query['id']}: {entry['institution']}/{entry['dataset']}"
            if dataset is None:
                problems.append(f"{where} not in the catalog")
                continue
            if dataset.veri_yok:
                problems.append(f"{where} is flagged veri_yok (never searchable)")
            for dimension_code, code in (entry.get("codes") or {}).items():
                exists = session.scalar(
                    sa.select(DimensionCode.id)
                    .join(DatasetDimension, DatasetDimension.id == DimensionCode.dimension_id)
                    .where(
                        DatasetDimension.dataset_id == dataset.id,
                        DatasetDimension.code == dimension_code,
                        DimensionCode.code == code,
                    )
                    .limit(1)
                )
                if exists is None:
                    problems.append(f"{where} has no code {dimension_code}={code}")
    return problems


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def render_report(
    queries: Mapping[str, Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    *,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """The Markdown report (Turkish headings, like the rest of the roadmap docs)."""
    lines = ["# Arama ölçümü (Task 2.6)", ""]
    if meta:
        lines += [f"- {key}: {value}" for key, value in meta.items()] + [""]
    verdict = "EŞİĞİ GEÇTİ" if summary["passes_threshold"] else "EŞİĞİN ALTINDA"
    lines += [
        "## Özet",
        "",
        f"- **İsabet oranı:** {_pct(summary['hit_rate'])} "
        f"({summary['hits']}/{summary['positive_runs']} olumlu koşu), "
        f"eşik {_pct(summary['threshold'])}: **{verdict}**",
        f"- Haber sorguları: {_pct(summary['groups']['haber']['hit_rate'])} "
        f"({summary['groups']['haber']['hits']}/{summary['groups']['haber']['runs']}), "
        f"ağaç sorguları: {_pct(summary['groups']['agac']['hit_rate'])} "
        f"({summary['groups']['agac']['hits']}/{summary['groups']['agac']['runs']})",
        f"- Sonuçlar: {summary['outcomes']}",
        "- İsabetli sonucun ortalama sırası: "
        + ("n/a" if summary["mean_rank_of_hit"] is None else f"{summary['mean_rank_of_hit']:.2f}"),
        f"- Olumsuz sorgularda yanlış pozitif: {summary['false_positives']}/"
        f"{summary['negative_runs']} ({_pct(summary['false_positive_rate'])}); "
        "isabet oranına dahil değildir",
        f"- Başarısız arama (`search_failed`, ıskalama sayılmaz): {summary['failed_runs']}",
    ]
    if summary["repeated_queries"]:
        flipped = ", ".join(summary["flipped_queries"]) or "yok"
        lines.append(
            f"- Tekrarlı koşulan sorgu: {summary['repeated_queries']}; "
            f"koşular arasında sonucu değişen: {flipped}"
        )
    lines += [
        "",
        "## Sorgu sorgu",
        "",
        "| id | grup | istek | sonuç | sıra | strong |",
        "|---|---|---|---|---|---|",
    ]
    by_query: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_query[row["id"]].append(row)
    for query_id, query in queries.items():
        runs = by_query.get(query_id, [])
        if not runs:
            lines.append(f"| {query_id} | {query['group']} | {query['request']} | koşulmadı | | |")
            continue
        outcomes = "/".join(run["outcome"] for run in runs)
        ranks = "/".join("-" if run["rank"] is None else str(run["rank"]) for run in runs)
        strong = "; ".join(f"{item['dataset']}" for item in runs[0]["strong"]) or "-"
        cells = [query_id, query["group"], query["request"], outcomes, ranks, strong]
        lines.append("| " + " | ".join(cells) + " |")
    misses = [row for row in rows if row["outcome"] in (MISS, CANDIDATE_ONLY, FALSE_POSITIVE)]
    if misses:
        lines += ["", "## Iskalar ve yanlış pozitifler (ayrıntı)", ""]
        for row in misses:
            query = queries[row["id"]]
            lines.append(f"### {row['id']}: {query['request']} ({row['outcome']})")
            expected = ", ".join(
                f"{entry['dataset']}{entry.get('codes') or ''}" for entry in query["expected"]
            )
            lines.append(f"- beklenen: {expected or '(hiçbiri)'}")
            lines.append(f"- strong: {[item['series'] for item in row['strong']]}")
            lines.append(f"- aday: {[item['series'] for item in row['candidates']]}")
            lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def read_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def run_queries(
    testset: Mapping[str, Any],
    out_dir: Path,
    *,
    only: Sequence[str] | None = None,
    repeat: int = 1,
    resume: bool = False,
) -> list[dict[str, Any]]:
    """Run the searches live and append one scored line per run to ``results.jsonl``."""
    from app.config import settings
    from app.db.session import SessionLocal
    from app.llm.chat import ChatClient
    from app.llm.jev import JevClient

    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done = Counter(row["id"] for row in read_results(results_path)) if resume else Counter()

    selected = [query for query in testset["queries"] if not only or query["id"] in only]
    jev = JevClient(settings)
    chat = ChatClient(settings)
    try:
        with SessionLocal() as session:
            ensure_default_prompts(session)
            session.commit()
            bodies, refs = load_search_prompts(session)
            for query in selected:
                for run in range(done[query["id"]] + 1, repeat + 1):
                    started = time.monotonic()
                    result = search_series(
                        session,
                        query["request"],
                        jev=jev,
                        chat=chat,
                        bodies=bodies,
                        refs=refs,
                        settings=settings,
                    )
                    scored = score_result(query, result)
                    scored["run"] = run
                    scored["seconds"] = round(time.monotonic() - started, 1)
                    with results_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(scored, ensure_ascii=False) + "\n")
                    print(
                        f"[{query['id']} run {run}] {scored['outcome']} "
                        f"strong={scored['strong_count']} {scored['seconds']}s",
                        flush=True,
                    )
    finally:
        jev.close()
        chat.close()
    return read_results(results_path)


def write_report(testset: Mapping[str, Any], out_dir: Path) -> Path:
    queries = {query["id"]: query for query in testset["queries"]}
    rows = [
        rescore_row(queries[row["id"]], row) if row["id"] in queries else row
        for row in read_results(out_dir / "results.jsonl")
    ]
    summary = summarize(rows, float(testset["threshold"]))
    seconds = [row["seconds"] for row in rows if "seconds" in row]
    changed = sorted(
        {row["id"] for row in rows if row.get("outcome_at_run", row["outcome"]) != row["outcome"]}
    )
    first = summarize(
        [
            {**row, "outcome": row.get("outcome_at_run", row["outcome"])}
            for row in rows
            if row.get("run", 1) == 1
        ],
        float(testset["threshold"]),
    )
    meta = {
        "tarih": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "ilk koşular, liste düzeltmesinden önce": _pct(first["hit_rate"]),
        "liste düzeltmesiyle sonucu değişen sorgu": ", ".join(changed) or "yok",
        "sorgu / koşu": f"{summary['queries']} / {summary['runs']}",
        "ortalama süre": f"{statistics.fmean(seconds):.1f} sn" if seconds else "n/a",
    }
    path = out_dir / "report.md"
    path.write_text(render_report(queries, rows, summary, meta=meta), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.search_eval",
        description="Search test set and measurement (Task 2.6).",
    )
    parser.add_argument("--testset", type=Path, default=TESTSET_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="check every expected entry against the catalog")
    run = commands.add_parser("run", help="run the searches live and write the report")
    run.add_argument("--only", nargs="*", help="query ids to run")
    run.add_argument("--repeat", type=int, default=1, help="runs per query (variance)")
    run.add_argument("--resume", action="store_true", help="skip runs already in results.jsonl")
    run.add_argument("--out", type=Path, help="output directory")
    report = commands.add_parser("report", help="rebuild report.md from results.jsonl")
    report.add_argument("out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    testset = load_testset(args.testset)

    if args.command == "validate":
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            problems = validate_against_catalog(session, testset)
        for problem in problems:
            print(problem)
        count = sum(len(query["expected"]) for query in testset["queries"])
        print(
            f"{len(testset['queries'])} queries, {count} expected entries, {len(problems)} problems"
        )
        return 1 if problems else 0

    if args.command == "report":
        print(write_report(testset, args.out))
        return 0

    out_dir = args.out or DEFAULT_OUT_ROOT / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_queries(testset, out_dir, only=args.only, repeat=args.repeat, resume=args.resume)
    report_path = write_report(testset, out_dir)
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"report: {report_path}")
    print(
        f"hit_rate={_pct(summary['hit_rate'])} threshold={_pct(summary['threshold'])} "
        f"passes={summary['passes_threshold']}"
    )
    return 0 if summary["passes_threshold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANDIDATE_ONLY",
    "CORRECT_EMPTY",
    "FAILED",
    "FALSE_POSITIVE",
    "HIT",
    "MISS",
    "TestsetError",
    "entry_matches",
    "load_testset",
    "render_report",
    "rescore_row",
    "run_queries",
    "score_result",
    "summarize",
    "validate_against_catalog",
    "write_report",
]
