"""CLI: ``python -m app.connectors.tuik``.

Commands:

- ``catalog [--limit N] [--dataflow ID] [--dry-run] [--verify-completeness]`` —
  sync datasets with their dimension code lists (no values). ``--dry-run``
  fetches and parses everything but writes nothing.
- ``fetch --dataset DF_X --code DIM=CODE [--code ...]`` or
  ``fetch --series EXTERNAL_CODE`` — ingest one series.
- ``find --text "..."`` — search dataset names and dimension code labels.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import (
    FORMAT_CHANGED,
    ROLE_TIME,
    ConnectorError,
    ingest_series,
    resolve_external_code,
    upsert_dataset,
    upsert_institution,
)
from app.connectors.tuik.client import Databrowser2Client
from app.connectors.tuik.connector import TuikConnector
from app.data.errors import SeriesDefinitionError, SeriesNotFoundError
from app.data.models import Dataset, DatasetDimension, DimensionCode, Institution

logger = logging.getLogger("app.connectors.tuik")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (pure, importable without a database)."""
    parser = argparse.ArgumentParser(
        prog="app.connectors.tuik",
        description="TÜİK databrowser2 connector (catalog, fetch, find).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="sync dataset/dimension metadata (no values)")
    catalog.add_argument("--limit", type=int, default=None, help="process at most N dataflows")
    catalog.add_argument("--dataflow", default=None, help="only this dataflow id or TR,ID,VER")
    catalog.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse every dataflow but write nothing (no DB, no MinIO)",
    )
    catalog.add_argument(
        "--verify-completeness",
        action="store_true",
        dest="verify_completeness",
        help="download cells and flag datasets that serve fewer rows than reported",
    )

    fetch = subparsers.add_parser("fetch", help="fetch and ingest one series")
    fetch.add_argument(
        "--dataset", default=None, help="dataset external code, e.g. DF_TUFE_SDMX_TT10"
    )
    fetch.add_argument(
        "--code",
        action="append",
        default=[],
        metavar="DIM=CODE",
        help="one dimension code; repeat for every non-time dimension",
    )
    fetch.add_argument("--series", default=None, help="external code, e.g. DF_X:TR.A.1")
    fetch.add_argument("--start", default="2000-01-01", help="first period start (YYYY-MM-DD)")
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse but write nothing (no DB, no MinIO)",
    )

    find = subparsers.add_parser("find", help="search dataset names and code labels")
    find.add_argument("--text", required=True, help="ILIKE pattern text")

    return parser


def _print_error(command: str, error: Exception) -> None:
    print(f"{command}: error: {error}", file=sys.stderr)


def _dataflows(connector: TuikConnector, args: argparse.Namespace):
    try:
        dataflows = connector.dataflows()
    except ConnectorError as exc:
        _print_error("catalog", exc)
        return None
    if args.dataflow:
        dataflows = [
            info
            for info in dataflows
            if info.dataflow_id == args.dataflow or info.dataset_identifier == args.dataflow
        ]
        if not dataflows:
            print(f"catalog: no dataflow matches {args.dataflow!r}", file=sys.stderr)
            return None
    if args.limit is not None:
        dataflows = dataflows[: args.limit]
    return dataflows


def _cmd_catalog(
    session: Session | None, connector: TuikConnector, args: argparse.Namespace
) -> int:
    started = time.monotonic()
    dataflows = _dataflows(connector, args)
    if dataflows is None:
        return 1
    if getattr(args, "verify_completeness", False):
        return _verify_completeness(session, connector, args, dataflows)

    dry_run = bool(getattr(args, "dry_run", False))
    institution_id: int | None = None
    if not dry_run:
        assert session is not None
        institution = upsert_institution(
            session, connector.institution_code, connector.institution_name
        )
        session.commit()
        institution_id = institution.id

    ok = 0
    outcomes: Counter[str] = Counter()
    failures: list[tuple[str, ConnectorError]] = []
    dimensions = 0
    codes = 0
    pending = deque(dataflows)

    with ThreadPoolExecutor(max_workers=max(1, connector.client.max_concurrency)) as pool:
        while pending:
            width = 1 if connector.client.throttled else max(1, connector.client.max_concurrency)
            batch = [pending.popleft() for _ in range(min(width, len(pending)))]
            futures = [(info, pool.submit(connector.dataset_meta, info)) for info in batch]
            for info, future in futures:
                try:
                    meta = future.result()
                except ConnectorError as exc:
                    if not dry_run and session is not None:
                        session.rollback()
                    failures.append((info.dataflow_id, exc))
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad dataflow must not abort the run
                    if not dry_run and session is not None:
                        session.rollback()
                    failures.append(
                        (
                            info.dataflow_id,
                            ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"),
                        )
                    )
                    continue
                dimensions += len(meta.dimensions)
                codes += sum(len(dimension.codes) for dimension in meta.dimensions)
                if dry_run:
                    outcomes["found"] += 1
                else:
                    assert session is not None and institution_id is not None
                    outcome, _ = upsert_dataset(session, institution_id, meta)
                    outcomes[outcome] += 1
                    session.commit()
                ok += 1

    runtime = time.monotonic() - started
    mode = " (dry-run)" if dry_run else ""
    print(f"catalog{mode}: {ok} dataflows ok, {len(failures)} failed, {len(dataflows)} processed")
    if dry_run:
        print(f"datasets: found={outcomes['found']} (dry-run, nothing written)")
    else:
        print(
            "datasets: "
            f"inserted={outcomes['inserted']} updated={outcomes['updated']} "
            f"unchanged={outcomes['unchanged']}"
        )
    print(f"dimensions={dimensions} codes={codes} runtime={runtime:.1f}s")
    _print_failures(failures)
    return 0


def _verify_completeness(
    session: Session | None,
    connector: TuikConnector,
    args: argparse.Namespace,
    dataflows: list,
) -> int:
    if session is None:
        print("catalog: --verify-completeness needs a database connection", file=sys.stderr)
        return 1
    started = time.monotonic()
    institution = upsert_institution(
        session, connector.institution_code, connector.institution_name
    )
    session.commit()
    flagged = 0
    ok = 0
    failures: list[tuple[str, ConnectorError]] = []
    for info in dataflows:
        try:
            result = connector.catalog_dataflow(info)
        except ConnectorError as exc:
            session.rollback()
            failures.append((info.dataflow_id, exc))
            continue
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            failures.append(
                (
                    info.dataflow_id,
                    ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"),
                )
            )
            continue
        if result.obs_expected and result.obs_received != result.obs_expected:
            dataset = session.scalar(
                select(Dataset).where(
                    Dataset.institution_id == institution.id,
                    Dataset.external_code == info.dataflow_id,
                )
            )
            if dataset is not None:
                dataset.source_incomplete = True
                dataset.source_incomplete_note = (
                    f"source reports {result.obs_expected}, serves {result.obs_received}"
                )
                session.commit()
                flagged += 1
                print(
                    f"{info.dataflow_id}: source reports {result.obs_expected}, "
                    f"serves {result.obs_received} (flagged)"
                )
        ok += 1
    runtime = time.monotonic() - started
    print(
        f"verify-completeness: {ok} checked, {flagged} flagged, "
        f"{len(failures)} failed, runtime={runtime:.1f}s"
    )
    _print_failures(failures)
    return 0


def _print_failures(failures: list[tuple[str, ConnectorError]]) -> None:
    by_kind: dict[str, list[str]] = {}
    for dataflow_id, exc in failures:
        by_kind.setdefault(exc.kind, []).append(dataflow_id)
    for kind in sorted(by_kind):
        ids = ", ".join(by_kind[kind])
        print(f"failed by kind: {kind} ({len(by_kind[kind])}): {ids}")
    for dataflow_id, exc in failures:
        print(f"  {dataflow_id}: {exc.kind}: {exc.message}", file=sys.stderr)


def _parse_codes(options: list[str]) -> dict[str, str]:
    codes: dict[str, str] = {}
    for option in options:
        dimension, separator, code = option.partition("=")
        if not separator or not dimension or not code:
            raise ValueError(f"--code must look like DIM=CODE, got {option!r}")
        codes[dimension] = code
    return codes


def _load_dataset(session: Session, connector: TuikConnector, external_code: str) -> Dataset | None:
    institution = session.scalar(
        select(Institution).where(Institution.code == connector.institution_code)
    )
    if institution is None:
        return None
    return session.scalar(
        select(Dataset).where(
            Dataset.institution_id == institution.id,
            Dataset.external_code == external_code,
        )
    )


def _dry_run_fetch(connector: TuikConnector, args: argparse.Namespace) -> int:
    try:
        start = date.fromisoformat(args.start)
    except ValueError:
        print(f"fetch: invalid --start {args.start!r} (want YYYY-MM-DD)", file=sys.stderr)
        return 1
    if args.series:
        dataset_code, separator, key = args.series.partition(":")
        if not separator or not key:
            _print_error("fetch", ValueError(f"bad --series {args.series!r}"))
            return 1
        parts: list[str] | None = key.split(".")
        codes: dict[str, str] = {}
    elif args.dataset:
        dataset_code = args.dataset
        parts = None
        try:
            codes = _parse_codes(args.code)
        except ValueError as exc:
            _print_error("fetch", exc)
            return 1
    else:
        print(
            "fetch: pass --series EXTERNAL_CODE or --dataset DF_X --code DIM=CODE",
            file=sys.stderr,
        )
        return 1

    try:
        meta = connector.dataset_meta(dataset_code)
        order = [
            dimension.code
            for dimension in sorted(meta.dimensions, key=lambda dim: dim.position)
            if dimension.role != ROLE_TIME
        ]
        if parts is not None:
            if len(parts) != len(order):
                _print_error(
                    "fetch",
                    ValueError(f"expected {len(order)} dimensions {order}, got {len(parts)} parts"),
                )
                return 1
            codes = dict(zip(order, parts, strict=True))
        result = connector.fetch_series(dataset_code, codes, order=order, start=start)
    except ConnectorError as exc:
        _print_error("fetch", exc)
        return 1
    print(
        f"fetch {dataset_code}: points={len(result.points)} "
        f"channel={result.channel} (dry-run, nothing written)"
    )
    if result.points:
        print(f"periods: {result.points[0][0].isoformat()}..{result.points[-1][0].isoformat()}")
    return 0


def _cmd_fetch(session: Session, connector: TuikConnector, args: argparse.Namespace) -> int:
    try:
        start = date.fromisoformat(args.start)
    except ValueError:
        print(f"fetch: invalid --start {args.start!r} (want YYYY-MM-DD)", file=sys.stderr)
        return 1
    try:
        if args.series:
            dataset_code = args.series.partition(":")[0]
            dataset = _load_dataset(session, connector, dataset_code)
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
            dataset = _load_dataset(session, connector, args.dataset)
            if dataset is None:
                print(f"fetch: no catalogued dataset {args.dataset!r}", file=sys.stderr)
                return 1
            codes = _parse_codes(args.code)
        else:
            print(
                "fetch: pass --series EXTERNAL_CODE or --dataset DF_X --code DIM=CODE",
                file=sys.stderr,
            )
            return 1
    except ValueError as exc:
        _print_error("fetch", exc)
        return 1

    try:
        result = ingest_series(session, connector, dataset=dataset, codes=codes, start=start)
    except (ConnectorError, SeriesDefinitionError, SeriesNotFoundError) as exc:
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


def _cmd_find(session: Session, connector: TuikConnector, args: argparse.Namespace) -> int:
    institution = session.scalar(
        select(Institution).where(Institution.code == connector.institution_code)
    )
    if institution is None:
        print("find: no catalogued institution yet; run `catalog` first")
        return 0
    pattern = f"%{args.text}%"
    datasets = session.scalars(
        select(Dataset)
        .where(Dataset.institution_id == institution.id, Dataset.name.ilike(pattern))
        .order_by(Dataset.external_code)
        .limit(50)
    ).all()
    for dataset in datasets:
        print(f"dataset\t{dataset.external_code}\t{dataset.name}")
    codes = session.execute(
        select(
            Dataset.external_code,
            DatasetDimension.code,
            DimensionCode.code,
            DimensionCode.label,
        )
        .join(DatasetDimension, DatasetDimension.dataset_id == Dataset.id)
        .join(DimensionCode, DimensionCode.dimension_id == DatasetDimension.id)
        .where(Dataset.institution_id == institution.id, DimensionCode.label.ilike(pattern))
        .order_by(Dataset.external_code, DatasetDimension.code, DimensionCode.code)
        .limit(50)
    ).all()
    for dataset_code, dimension, code, label in codes:
        print(f"code\t{dataset_code}\t{dimension}={code}\t{label}")
    print(f"find: {len(datasets)} datasets, {len(codes)} codes shown (limit 50 each)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        connector = TuikConnector(client=Databrowser2Client(store=None))
    else:
        connector = TuikConnector()
    try:
        if dry_run and args.command == "catalog":
            return _cmd_catalog(None, connector, args)
        if dry_run and args.command == "fetch":
            return _dry_run_fetch(connector, args)

        from app.db.session import SessionLocal

        handlers = {"catalog": _cmd_catalog, "fetch": _cmd_fetch, "find": _cmd_find}
        with SessionLocal() as session:
            return handlers[args.command](session, connector, args)
    finally:
        connector.close()


if __name__ == "__main__":
    raise SystemExit(main())
