"""Command line: test one relation on the loaded data (Task 3.2).

    python -m app.analysis run --target "tuik|<code>" --driver "tcmb|<code>:positive" \\
        --lags 0 3 --transform annual_pct_change [--nominal "tuik|<code>"] [--json]

Series are ``institution|external_code``. Nothing is fetched: unloaded series are reported
as missing data. The result is printed, never written anywhere.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Any

from app.analysis.models import MissingData, RelationSpec, TestReport, Unsupported
from app.analysis.runner import run_relation
from app.analysis.significance import HacOls
from app.analysis.transforms import TRANSFORMS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.analysis", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="test one relation on the loaded data")
    run.add_argument("--target", required=True, help="institution|external_code")
    run.add_argument(
        "--driver",
        action="append",
        required=True,
        metavar="KEY:DIRECTION",
        help="institution|external_code:positive|negative (repeat for several drivers)",
    )
    run.add_argument("--lags", nargs=2, type=int, default=(0, 3), metavar=("MIN", "MAX"))
    run.add_argument("--transform", choices=TRANSFORMS, default="annual_pct_change")
    run.add_argument("--nominal", action="append", default=[], help="nominal TL series key")
    run.add_argument(
        "--method",
        choices=("experimental", "hac"),
        default="experimental",
        help="experimental = whole-chain bootstrap (block 24, B=499), NOT validated; hac = OLS+HAC",
    )
    run.add_argument("--maxlags", type=int, default=None, help="force the HAC maxlags (hac only)")
    run.add_argument("--lag-correction", action="store_true", help="Bonferroni over the lags")
    run.add_argument("--json", action="store_true", help="print JSON")
    return parser


def parse_spec(args: argparse.Namespace) -> RelationSpec:
    directions: dict[str, str] = {}
    for item in args.driver:
        key, _, direction = item.rpartition(":")
        directions[key] = direction
    return RelationSpec(
        target=args.target,
        driver_directions=directions,
        lag_min=args.lags[0],
        lag_max=args.lags[1],
        transform=args.transform,
        nominal_tl_series=tuple(args.nominal),
    )


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _plain(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = parse_spec(args)
    from app.db.session import SessionLocal

    method = (
        HacOls(maxlags=args.maxlags, lag_correction=args.lag_correction)
        if args.method == "hac"
        else None  # the experimental defaults of the runner
    )
    with SessionLocal() as session:
        outcome = run_relation(session, spec, method=method)
    if isinstance(outcome, MissingData):
        print("missing data (not loaded): " + ", ".join(outcome.series), file=sys.stderr)
        return 2
    if isinstance(outcome, Unsupported):
        print("unsupported series: " + json.dumps(dict(outcome.reasons)), file=sys.stderr)
        return 3
    assert isinstance(outcome, TestReport)
    if args.json:
        print(json.dumps(_plain(outcome), default=str, indent=2, ensure_ascii=False))
        return 0
    print(f"status: {outcome.status} (reliable: {outcome.reliable}) transform: {outcome.transform}")
    for period, window in outcome.windows.items():
        print(
            f"  {period}: {window.result} n={window.n_obs} lag={window.best_lag} "
            f"{window.statistic_name}={window.statistic_value} p={window.p_value} "
            f"{window.start}..{window.end}"
        )
        if not window.reliable:
            print(f"    warning: {window.reliability_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
