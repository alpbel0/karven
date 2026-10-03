"""CLI: ``python -m app.connectors.tcmb``.

Commands:

- ``catalog [--source {tcmb,hmb}] [--limit N] [--group CODE] [--dry-run]`` — sync
  the institution and its datagroups (datasets/dimensions/codes; no values).
  ``--dry-run`` fetches and parses everything but writes nothing.
- ``fetch [--source {tcmb,hmb}] --series bie_dkefkytl:TP.DK.USD.A.EF.YTL`` or
  ``fetch --dataset G --code SERIE=CODE [--start YYYY-MM-DD]`` — ingest one series.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import (
    THROTTLED,
    ConnectorError,
    MinioObjectStore,
    ingest_series,
    resolve_external_code,
    sync_catalog,
)
from app.connectors.tcmb.client import EvdsClient
from app.connectors.tcmb.connector import SERIE_DIMENSION, SOURCES, TcmbConnector
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset, Institution

logger = logging.getLogger("app.connectors.tcmb")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (pure, importable without a database)."""
    parser = argparse.ArgumentParser(
        prog="app.connectors.tcmb",
        description="TCMB EVDS3 connector (catalog, fetch).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="sync dataset/dimension metadata (no values)")
    catalog.add_argument(
        "--source",
        choices=tuple(SOURCES),
        default="tcmb",
        help="institution to sync (default tcmb)",
    )
    catalog.add_argument("--limit", type=int, default=None, help="process at most N groups")
    catalog.add_argument("--group", default=None, help="only this DATAGROUP_CODE")
    catalog.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse every group but write nothing (no DB, no MinIO)",
    )

    fetch = subparsers.add_parser("fetch", help="fetch and ingest one series")
    fetch.add_argument(
        "--source",
        choices=tuple(SOURCES),
        default="tcmb",
        help="institution to fetch (default tcmb)",
    )
    fetch.add_argument("--dataset", default=None, help="datagroup code, e.g. bie_dkefkytl")
    fetch.add_argument(
        "--code",
        action="append",
        default=[],
        metavar="DIM=CODE",
        help="one dimension code (SERIE=...)",
    )
    fetch.add_argument(
        "--series", default=None, help="external code, e.g. bie_dkefkytl:TP.DK.USD.A.EF.YTL"
    )
    fetch.add_argument("--start", default="2000-01-01", help="first period start (YYYY-MM-DD)")
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse but write nothing (no DB, no MinIO)",
    )
    return parser


def _print_error(command: str, error: Exception) -> None:
    print(f"{command}: error: {error}", file=sys.stderr)


def _parse_codes(options: list[str]) -> dict[str, str]:
    codes: dict[str, str] = {}
    for option in options:
        dimension, separator, code = option.partition("=")
        if not separator or not dimension or not code:
            raise ValueError(f"--code must look like DIM=CODE, got {option!r}")
        codes[dimension] = code
    return codes


def _parse_start(args: argparse.Namespace) -> date | None:
    try:
        return date.fromisoformat(args.start)
    except ValueError:
        print(f"fetch: invalid --start {args.start!r} (want YYYY-MM-DD)", file=sys.stderr)
        return None


def _build_connector(
    dry_run: bool, *, source: str = "tcmb", limit: int | None = None, group: str | None = None
) -> TcmbConnector:
    """Create the connector; a dry run never touches MinIO."""
    store = None if dry_run else MinioObjectStore()
    client = EvdsClient(store=store, institution=SOURCES[source].institution_code)
    return TcmbConnector(client=client, source=source, limit=limit, only_group=group)


def _cmd_catalog(
    session: Session | None, connector: TcmbConnector, args: argparse.Namespace
) -> int:
    started = time.monotonic()
    if args.dry_run:
        try:
            metas = list(connector.list_datasets())
        except ConnectorError as exc:
            _print_error("catalog", exc)
            return 1
        series_count = 0
        empty = 0
        frequencies: Counter[str] = Counter()
        aggregations: Counter[str] = Counter()
        for meta in metas:
            codes = [code for dimension in meta.dimensions for code in dimension.codes]
            series_count += len(codes)
            if not codes:
                empty += 1
            for code in codes:
                frequencies[str(code.attributes.get("frequency"))] += 1
                aggregations[str(code.attributes.get("aggregation"))] += 1
        runtime = time.monotonic() - started
        print(
            f"catalog (dry-run, source={connector.source}): groups={len(metas)} "
            f"series={series_count} empty={empty} runtime={runtime:.1f}s"
        )
        print("frequencies: " + _counter_line(frequencies))
        print("aggregations: " + _counter_line(aggregations))
        return _report_failed_groups(connector)

    assert session is not None
    try:
        result = sync_catalog(session, connector)
    except ConnectorError as exc:
        session.rollback()
        _print_error("catalog", exc)
        return 1
    session.commit()
    runtime = time.monotonic() - started
    print(
        f"catalog (source={connector.source}): inserted={result.inserted} "
        f"updated={result.updated} unchanged={result.unchanged}"
    )
    print(f"dimensions={result.dimensions} codes={result.codes} runtime={runtime:.1f}s")
    return _report_failed_groups(connector)


def _report_failed_groups(connector: TcmbConnector) -> int:
    """Print every group whose series list failed; a partial catalog exits non-zero."""
    if not connector.failed_groups:
        return 0
    for code, kind, message in connector.failed_groups:
        print(f"catalog: group {code} failed ({kind}): {message}", file=sys.stderr)
    print(
        f"catalog: {len(connector.failed_groups)} group(s) failed; catalog is incomplete",
        file=sys.stderr,
    )
    return 1


def _counter_line(counter: Counter[str]) -> str:
    return ", ".join(f"{key}={count}" for key, count in sorted(counter.items())) or "-"


def _dry_run_fetch(connector: TcmbConnector, args: argparse.Namespace) -> int:
    start = _parse_start(args)
    if start is None:
        return 1
    if args.series:
        dataset_code, separator, key = args.series.partition(":")
        if not separator or not key:
            _print_error("fetch", ValueError(f"bad --series {args.series!r}"))
            return 1
        codes = {SERIE_DIMENSION: key}
    elif args.dataset:
        dataset_code = args.dataset
        try:
            codes = _parse_codes(args.code)
        except ValueError as exc:
            _print_error("fetch", exc)
            return 1
    else:
        print(
            "fetch: pass --series EXTERNAL_CODE or --dataset G --code SERIE=CODE",
            file=sys.stderr,
        )
        return 1

    try:
        result = connector.fetch_series(dataset_code, codes, start=start)
    except ConnectorError as exc:
        if exc.kind == THROTTLED:
            print("throttled: stop and report", file=sys.stderr)
            return 1
        _print_error("fetch", exc)
        return 1
    print(
        f"fetch {dataset_code}: points={len(result.points)} "
        f"channel={result.channel} (dry-run, nothing written)"
    )
    if result.points:
        print(f"periods: {result.points[0][0].isoformat()}..{result.points[-1][0].isoformat()}")
    return 0


def _load_dataset(session: Session, external_code: str, institution_code: str) -> Dataset | None:
    institution = session.scalar(select(Institution).where(Institution.code == institution_code))
    if institution is None:
        return None
    return session.scalar(
        select(Dataset).where(
            Dataset.institution_id == institution.id,
            Dataset.external_code == external_code,
        )
    )


def _cmd_fetch(session: Session, connector: TcmbConnector, args: argparse.Namespace) -> int:
    start = _parse_start(args)
    if start is None:
        return 1
    institution_code = connector.institution_code
    try:
        if args.series:
            dataset_code = args.series.partition(":")[0]
            dataset = _load_dataset(session, dataset_code, institution_code)
            if dataset is None:
                print(f"fetch: no catalogued dataset for {dataset_code!r}", file=sys.stderr)
                return 1
            codes = resolve_external_code(dataset, args.series)
            if codes is None:
                print(
                    f"fetch: {args.series!r} does not match dataset {dataset_code!r}",
                    file=sys.stderr,
                )
                return 1
        elif args.dataset:
            dataset = _load_dataset(session, args.dataset, institution_code)
            if dataset is None:
                print(f"fetch: no catalogued dataset {args.dataset!r}", file=sys.stderr)
                return 1
            codes = _parse_codes(args.code)
        else:
            print(
                "fetch: pass --series EXTERNAL_CODE or --dataset G --code SERIE=CODE",
                file=sys.stderr,
            )
            return 1
    except ValueError as exc:
        _print_error("fetch", exc)
        return 1

    try:
        result = ingest_series(session, connector, dataset=dataset, codes=codes, start=start)
    except ConnectorError as exc:
        if exc.kind == THROTTLED:
            print("throttled: stop and report", file=sys.stderr)
            return 1
        _print_error("fetch", exc)
        return 1
    except SeriesDefinitionError as exc:
        _print_error("fetch", exc)
        return 1
    session.commit()
    print(
        f"fetch {result.series_id}: inserted={result.inserted} unchanged={result.unchanged} "
        f"points={result.point_count}"
    )
    if result.period_start is not None:
        print(f"periods: {result.period_start.isoformat()}..{result.period_end.isoformat()}")
    if result.raw_object_key is not None:
        print(f"raw: {result.raw_object_key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    dry_run = bool(getattr(args, "dry_run", False))
    connector = _build_connector(
        dry_run,
        source=getattr(args, "source", "tcmb"),
        limit=getattr(args, "limit", None),
        group=getattr(args, "group", None),
    )
    try:
        if args.command == "catalog":
            if dry_run:
                return _cmd_catalog(None, connector, args)
            from app.db.session import SessionLocal

            with SessionLocal() as session:
                return _cmd_catalog(session, connector, args)
        if dry_run:
            return _dry_run_fetch(connector, args)
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            return _cmd_fetch(session, connector, args)
    finally:
        connector.close()


if __name__ == "__main__":
    raise SystemExit(main())
