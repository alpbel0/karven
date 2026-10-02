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
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import (
    FORMAT_CHANGED,
    NOT_FOUND,
    ROLE_TIME,
    ConnectorError,
    MinioObjectStore,
    ensure_series,
    ingest_series,
    resolve_external_code,
    upsert_dataset,
    upsert_institution,
)
from app.connectors.tuik.bi_trade import (
    HEADLINE_BY_NAME,
    HEADLINES,
    SYSTEM_KEYS,
    BiTradeClient,
    BiTradeConnector,
    all_systems,
    complete_codes,
    dimension_codes,
    get_system,
    validate_codes,
)
from app.connectors.tuik.cip import CipClient, CipConnector
from app.connectors.tuik.client import Databrowser2Client
from app.connectors.tuik.connector import TuikConnector
from app.connectors.tuik.nsiws import build_nsiws_client
from app.connectors.tuik.parsers import DataflowInfo
from app.connectors.tuik.siniflama import (
    ClassificationData,
    CorrespondenceItemRow,
    CorrespondenceSummary,
    DimensionLinkRow,
    SiniflamaClient,
    SiniflamaVersion,
    discover_versions,
    fetch_classification,
    link_dimensions,
    load_classifications,
    load_correspondences,
    parse_correspondence_detail,
    parse_correspondence_list,
)
from app.connectors.tuik.turcat import TurcatClient, TurcatConnector, ingest_sector
from app.connectors.tuik.turcat_parsers import SECTORS
from app.data.errors import SeriesDefinitionError, SeriesNotFoundError
from app.data.models import Dataset, DatasetDimension, DimensionCode, Institution
from app.data.observations import record_observations

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

    turcat = subparsers.add_parser(
        "turcat", help="sync the IMF SDDS (Turcat) sector indicators and values"
    )
    turcat.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse the five sectors but write nothing (no DB, no MinIO)",
    )

    cip = subparsers.add_parser(
        "cip-catalog", help="sync CİP regional datasets (datasets/dimensions/codes; no values)"
    )
    cip.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse every indicator but write nothing (no DB, no MinIO)",
    )
    cip.add_argument("--limit", type=int, default=None, help="process at most N indicators")
    cip.add_argument(
        "--kaynak",
        default=None,
        choices=["medas", "ilGostergeleri", "json"],
        help="only indicators of this sideMenu kaynak",
    )

    siniflama = subparsers.add_parser(
        "siniflama", help="sync the TÜİK classification server (versions + correspondences)"
    )
    siniflama.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse everything but write nothing (no DB, no MinIO)",
    )
    siniflama.add_argument(
        "--only-id", default=None, help="process only this classification version id"
    )
    siniflama.add_argument(
        "--no-correspondences",
        action="store_true",
        dest="no_correspondences",
        help="skip the correspondence tables",
    )

    siniflama_link = subparsers.add_parser(
        "siniflama-link-dimensions",
        help="link databrowser2 dimensions to classification versions by normalized code/label",
    )
    siniflama_link.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="read and report the matches but write nothing",
    )
    siniflama_link.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="write a TSV of every qualifying link plus the classification-like near-misses",
    )

    bi_catalog = subparsers.add_parser(
        "bi-catalog",
        help="sync the bi.tuik Qlik foreign-trade datasets (GTS/ÖTS) with full code lists",
    )
    bi_catalog.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse everything but write nothing (no DB, no MinIO)",
    )
    bi_catalog.add_argument(
        "--system", default=None, choices=list(SYSTEM_KEYS), help="only this trade system"
    )

    bi_headlines = subparsers.add_parser(
        "bi-headlines",
        help="load the preconfigured bi.tuik headline series (MEASURE=USD, monthly)",
    )
    bi_headlines.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and split everything but write nothing (no DB, no MinIO)",
    )
    bi_headlines.add_argument(
        "--system", default=None, choices=list(SYSTEM_KEYS), help="only this trade system"
    )
    bi_headlines.add_argument(
        "--only", default=None, help="only this headline: " + ", ".join(HEADLINE_BY_NAME)
    )

    return parser


def _print_error(command: str, error: Exception) -> None:
    print(f"{command}: error: {error}", file=sys.stderr)


def _dataflows(connector: TuikConnector, args: argparse.Namespace):
    try:
        # Listed ones first, then the known ones the listing dropped.
        dataflows = [*connector.dataflows(), *connector.unlisted_dataflows()]
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
    hidden_gained = 0
    unverified = 0
    listed = unlisted = removed = 0
    today = date.today().isoformat()
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
                    if not info.listed and exc.kind == NOT_FOUND:
                        # Gone from the listing and the source no longer answers.
                        logger.warning(
                            "tuik catalog: %s left the listing and the source no longer "
                            "serves it; marking removed",
                            info.dataflow_id,
                        )
                        removed += 1
                        if not dry_run:
                            assert session is not None and institution_id is not None
                            _mark_removed(session, institution_id, info.dataflow_id, today)
                            session.commit()
                        continue
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
                if info.listed:
                    listed += 1
                else:
                    unlisted += 1
                    meta.attributes["unlisted_since"] = _unlisted_since(
                        session if not dry_run else None,
                        institution_id,
                        info.dataflow_id,
                        today,
                    )
                dimensions += len(meta.dimensions)
                codes += sum(len(dimension.codes) for dimension in meta.dimensions)
                if any(dim.attributes.get("from_hidden") for dim in meta.dimensions):
                    hidden_gained += 1
                if not meta.attributes.get("data_dimensions_verified", True):
                    unverified += 1
                if dry_run and args.dataflow:
                    _print_dimensions(info.dataflow_id, meta)
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
    print(f"dataflows: listed={listed} unlisted={unlisted} removed={removed}")
    print(
        f"hidden-dimensions catalogued: {hidden_gained} datasets; "
        f"unverified data dimensions: {unverified} datasets"
    )
    _print_failures(failures)
    return 0


def _existing_dataset(
    session: Session | None, institution_id: int | None, dataflow_id: str
) -> Dataset | None:
    if session is None or institution_id is None:
        return None
    return session.scalar(
        select(Dataset).where(
            Dataset.institution_id == institution_id,
            Dataset.external_code == dataflow_id,
        )
    )


def _unlisted_since(
    session: Session | None, institution_id: int | None, dataflow_id: str, today: str
) -> str:
    """The earliest date the dataflow was seen missing (kept once recorded)."""
    dataset = _existing_dataset(session, institution_id, dataflow_id)
    if dataset is not None:
        recorded = (dataset.attributes or {}).get("unlisted_since")
        if recorded:
            return min(str(recorded), today)
    return today


def _mark_removed(session: Session, institution_id: int, dataflow_id: str, today: str) -> None:
    """Flag a dataset the source no longer serves; dimensions stay untouched."""
    dataset = _existing_dataset(session, institution_id, dataflow_id)
    if dataset is None:
        return
    attributes = dict(dataset.attributes or {})
    attributes["removed_at"] = min(str(attributes.get("removed_at") or today), today)
    attributes.setdefault("unlisted_since", today)
    dataset.attributes = attributes


def load_known_dataflows(session: Session, institution_code: str) -> list[DataflowInfo]:
    """Databrowser2 datasets already in the database, as dataflows (read-only)."""
    rows = session.execute(
        select(Dataset)
        .join(Institution, Institution.id == Dataset.institution_id)
        .where(Institution.code == institution_code)
    ).scalars()
    known: list[DataflowInfo] = []
    for dataset in rows:
        attributes = dataset.attributes or {}
        if attributes.get("channel") != "databrowser2":
            continue
        agency, version = attributes.get("agency"), attributes.get("version")
        if not agency or not version:
            continue
        known.append(
            DataflowInfo(
                dataflow_id=str(attributes.get("dataflow_id") or dataset.external_code),
                version=str(version),
                agency=str(agency),
                title=dataset.name,
                description=dataset.description,
                source_category=dataset.source_category,
                listed=False,
            )
        )
    return known


def _known_for(command: str, dry_run: bool) -> list[DataflowInfo]:
    """Known dataflows for ``catalog``/``fetch``; a dry run tolerates an unreachable DB."""
    if command not in ("catalog", "fetch"):
        return []
    try:
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            return load_known_dataflows(session, TuikConnector.institution_code)
    except Exception:  # noqa: BLE001
        if not dry_run:
            raise
        logger.warning("tuik: database unreachable, unlisted known dataflows unavailable")
        return []


def _print_dimensions(dataflow_id: str, meta: Any) -> None:
    """Print one dataset's catalogued dimensions (dry-run, single dataflow)."""
    print(f"{dataflow_id}: dimensions={len(meta.dimensions)}")
    for dim in sorted(meta.dimensions, key=lambda item: item.position):
        flags = []
        if dim.role == ROLE_TIME:
            flags.append("time")
        if dim.attributes.get("from_hidden"):
            flags.append("from_hidden")
        marker = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {dim.position}: {dim.code} codes={len(dim.codes)}{marker}")
    if meta.source_incomplete:
        print(f"  source_incomplete: {meta.source_incomplete_note}")


def _fetch_correspondence_detail(
    client: SiniflamaClient, summary: CorrespondenceSummary
) -> list[CorrespondenceItemRow]:
    detail = client.correspondence_detail(
        summary.external_id, summary.from_external_id, summary.to_external_id
    )
    return parse_correspondence_detail(detail.json())


def _stream_classifications(
    client: SiniflamaClient, versions: list[SiniflamaVersion], *, workers: int
) -> Iterator[tuple[SiniflamaVersion, ClassificationData | None, ConnectorError | None]]:
    """Fetch+parse each version, keeping at most ``workers`` fetches in flight.

    Yields ``(version, data, None)`` for a success and ``(version, None, error)``
    for a failure; never holds more parsed versions than are in flight, so a
    memory-hungry version cannot pile up behind a slow load.
    """
    queue = deque(versions)
    in_flight: deque[tuple[SiniflamaVersion, Future[ClassificationData]]] = deque()
    width = max(1, workers)
    with ThreadPoolExecutor(max_workers=width) as pool:
        while queue or in_flight:
            while queue and len(in_flight) < width:
                version = queue.popleft()
                in_flight.append((version, pool.submit(fetch_classification, client, version)))
            version, future = in_flight.popleft()
            try:
                yield version, future.result(), None
            except ConnectorError as exc:
                yield version, None, exc
            except Exception as exc:  # noqa: BLE001 - one bad version must not abort the run
                yield version, None, ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}")


def _stream_correspondences(
    client: SiniflamaClient, summaries: list[CorrespondenceSummary], *, workers: int
) -> Iterator[
    tuple[CorrespondenceSummary, list[CorrespondenceItemRow] | None, ConnectorError | None]
]:
    """Fetch+parse each correspondence table with the same bounded concurrency."""
    queue = deque(summaries)
    in_flight: deque[tuple[CorrespondenceSummary, Future[list[CorrespondenceItemRow]]]] = deque()
    width = max(1, workers)
    with ThreadPoolExecutor(max_workers=width) as pool:
        while queue or in_flight:
            while queue and len(in_flight) < width:
                summary = queue.popleft()
                in_flight.append(
                    (summary, pool.submit(_fetch_correspondence_detail, client, summary))
                )
            summary, future = in_flight.popleft()
            try:
                yield summary, future.result(), None
            except ConnectorError as exc:
                yield summary, None, exc
            except Exception as exc:  # noqa: BLE001 - one bad table must not abort the run
                yield summary, None, ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}")


def _cmd_siniflama(
    session: Session | None, client: SiniflamaClient, args: argparse.Namespace
) -> int:
    started = time.monotonic()
    dry_run = session is None
    only_id = getattr(args, "only_id", None)
    no_correspondences = bool(getattr(args, "no_correspondences", False))

    versions, list_failures = discover_versions(client)
    if only_id is not None:
        versions = [version for version in versions if version.external_id == str(only_id)]
        if not versions:
            print(f"siniflama: no version with id {only_id!r} in the listing", file=sys.stderr)
            return 1

    workers = max(1, client.max_concurrency)
    version_failures: list[tuple[str, ConnectorError]] = []
    parent_missing = total_items = 0
    ok_versions = 0
    inserted = updated = unchanged = removed = 0
    # Fetch -> parse -> upsert -> commit per version, so a failure (fetch or
    # write) is reported and never rolls back a version already committed.
    for version, data, error in _stream_classifications(client, versions, workers=workers):
        if error is not None:
            version_failures.append((version.external_id, error))
            continue
        assert data is not None
        found = len(data.items)
        missing = sum(1 for item in data.items if item.attributes.get("parent_missing"))
        if dry_run:
            ok_versions += 1
            total_items += found
            parent_missing += missing
            continue
        assert session is not None
        try:
            loads = load_classifications(session, [data])
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one bad version must not abort the run
            session.rollback()
            version_failures.append(
                (
                    version.external_id,
                    ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"),
                )
            )
            continue
        ok_versions += 1
        total_items += found
        parent_missing += missing
        for load in loads:
            inserted += load.items_inserted
            updated += load.items_updated
            unchanged += load.items_unchanged
            removed += load.items_removed

    corr_ok = corr_failed = corr_rows = 0
    failures: list[tuple[str, ConnectorError]] = [*list_failures, *version_failures]
    if not no_correspondences:
        summaries: list[CorrespondenceSummary] = []
        try:
            response = client.correspondence_list()
            summaries = parse_correspondence_list(response.json())
        except ConnectorError as exc:
            corr_failed += 1
            failures.append(("correspondences", exc))
        except Exception as exc:  # noqa: BLE001 - one bad listing must not abort the run
            corr_failed += 1
            failures.append(
                ("correspondences", ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"))
            )
        for summary, items, error in _stream_correspondences(client, summaries, workers=workers):
            if error is not None:
                corr_failed += 1
                failures.append((summary.external_id, error))
                continue
            assert items is not None
            if dry_run:
                corr_ok += 1
                corr_rows += len(items)
                continue
            assert session is not None
            try:
                load_correspondences(session, [(summary, items)])
                session.commit()
            except Exception as exc:  # noqa: BLE001 - one bad table must not abort the run
                session.rollback()
                corr_failed += 1
                failures.append(
                    (
                        summary.external_id,
                        ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"),
                    )
                )
                continue
            corr_ok += 1
            corr_rows += len(items)

    runtime = time.monotonic() - started
    mode = " (dry-run)" if dry_run else ""
    failed_versions = len(list_failures) + len(version_failures)
    print(
        f"siniflama{mode}: versions ok={ok_versions} failed={failed_versions} "
        f"parent_missing={parent_missing}"
    )
    if dry_run:
        print(
            f"items: found={total_items} "
            f"inserted=0 updated=0 unchanged=0 removed=0 (dry-run, nothing written)"
        )
    else:
        print(
            f"items: inserted={inserted} updated={updated} unchanged={unchanged} removed={removed}"
        )
    print(f"correspondences: ok={corr_ok} failed={corr_failed} rows={corr_rows}")
    print(f"runtime={runtime:.1f}s")
    _print_failures(failures)
    return 0


_REPORT_HEADER = "status\tdataset\tdimension\tclassification\ttotal\tcoverage\tlabel_agreement\n"


def _write_link_report(path: str, rows: tuple[DimensionLinkRow, ...]) -> None:
    """Write the link report as TSV: qualifying links then per-dimension near-misses."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(_REPORT_HEADER)
        for row in rows:
            status = "link" if row.qualified else "near_miss"
            handle.write(
                f"{status}\t{row.dataset}\t{row.dimension}\t{row.classification}\t"
                f"{row.total}\t{row.coverage:.3f}\t{row.label_agreement:.3f}\n"
            )


def _cmd_siniflama_link(session: Session | None, args: argparse.Namespace) -> int:
    assert session is not None
    dry_run = bool(getattr(args, "dry_run", False))
    report_path = getattr(args, "report", None)
    result = link_dimensions(session, dry_run=dry_run, collect_report=bool(report_path))
    if not dry_run:
        session.commit()
    mode = " (dry-run)" if dry_run else ""
    print(
        f"siniflama-link-dimensions{mode}: dimensions={result.dimensions} links={result.links} "
        f"inserted={result.inserted} updated={result.updated} deleted={result.deleted} "
        f"unchanged={result.unchanged}"
    )
    if report_path:
        _write_link_report(report_path, result.report)
        print(f"report: {report_path} ({len(result.report)} rows)")
    return 0


def _run_siniflama(args: argparse.Namespace, dry_run: bool) -> int:
    if args.command == "siniflama-link-dimensions":
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            return _cmd_siniflama_link(session, args)

    client = SiniflamaClient(store=None if dry_run else MinioObjectStore())
    try:
        if dry_run:
            return _cmd_siniflama(None, client, args)
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            return _cmd_siniflama(session, client, args)
    finally:
        client.close()


def _cmd_cip_catalog(
    session: Session | None, connector: CipConnector, args: argparse.Namespace
) -> int:
    started = time.monotonic()
    entries = connector.entries()
    if args.kaynak:
        entries = [entry for entry in entries if entry.kaynak == args.kaynak]
    if args.limit is not None:
        entries = entries[: args.limit]

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
    level_histogram: Counter[tuple[int, ...]] = Counter()
    dimensions = codes = 0
    unexpected: list[tuple[str, ConnectorError]] = []
    max_workers = max(1, connector.client.max_concurrency)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [(entry, pool.submit(connector.dataset_meta, entry)) for entry in entries]
        for entry, future in futures:
            try:
                meta = future.result()
            except ConnectorError as exc:
                if not dry_run and session is not None:
                    session.rollback()
                unexpected.append((f"CIP_{entry.gosterge_no}", exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one bad indicator must not abort the run
                if not dry_run and session is not None:
                    session.rollback()
                unexpected.append(
                    (
                        f"CIP_{entry.gosterge_no}",
                        ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"),
                    )
                )
                continue
            if not meta.dimensions:
                # No level returned data; the connector already recorded the reason.
                # Persist it as a flagged dataset ("record it as a failed dataset").
                if not dry_run:
                    assert session is not None and institution_id is not None
                    outcome, _ = upsert_dataset(session, institution_id, meta)
                    outcomes[outcome] += 1
                    session.commit()
                continue
            working = tuple(meta.attributes.get("working_levels") or ())
            level_histogram[working] += 1
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

    catalog_failures = connector.failures()
    failure_rows = [
        (f"CIP_{failure.gosterge_no}", ConnectorError(failure.kind, failure.reason))
        for failure in catalog_failures
    ] + unexpected

    runtime = time.monotonic() - started
    mode = " (dry-run)" if dry_run else ""
    print(
        f"cip-catalog{mode}: {ok} datasets ok, {len(failure_rows)} failed, {len(entries)} processed"
    )
    if dry_run:
        print(f"datasets: found={outcomes['found']} (dry-run, nothing written)")
    else:
        print(
            "datasets: "
            f"inserted={outcomes['inserted']} updated={outcomes['updated']} "
            f"unchanged={outcomes['unchanged']}"
        )
    histogram = ", ".join(
        f"{'/'.join(str(level) for level in levels) or '-'}={count}"
        for levels, count in sorted(level_histogram.items())
    )
    print(f"levels/dataset: {histogram or '-'}")
    print(f"dimensions={dimensions} codes={codes} runtime={runtime:.1f}s")
    _print_failures(failure_rows)
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


def _cmd_turcat(
    session: Session | None, connector: TurcatConnector, args: argparse.Namespace
) -> int:
    dry_run = bool(getattr(args, "dry_run", False))
    started = time.monotonic()
    codes = [entry[0] for entry in SECTORS]
    fetched: dict[str, object] = {}
    failures: list[tuple[str, ConnectorError]] = []
    workers = max(1, min(len(codes), connector.client.max_concurrency))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {code: pool.submit(connector.fetch_sector, code) for code in codes}
        for code, future in futures.items():
            try:
                fetched[code] = future.result()
            except ConnectorError as exc:
                failures.append((code, exc))
            except Exception as exc:  # noqa: BLE001 - one bad sector must not abort the run
                failures.append(
                    (code, ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"))
                )

    total_indicators = total_values = total_unparsed = 0
    samples: list[str] = []
    for code in codes:
        fetch = fetched.get(code)
        if fetch is None:
            continue
        parsed = fetch.parse
        values = sum(1 for item in parsed.indicators if item.latest_value is not None)
        total_indicators += len(parsed.indicators)
        total_values += values
        total_unparsed += len(parsed.errors)
        print(
            f"sector {code}: rows={len(parsed.indicators) + len(parsed.groups)} "
            f"indicators={len(parsed.indicators)} groups={len(parsed.groups)} "
            f"values={values} unparsed={len(parsed.errors)}"
        )
        for error in parsed.errors[:5]:
            print(f"  unparseable: {error}")
        for item in parsed.indicators:
            if item.latest_value is None:
                continue
            samples.append(
                f"  {code}:{item.code} {item.name[:44]!r} period={item.period} "
                f"prev={item.previous_period} latest={item.latest_value} "
                f"previous={item.previous_value} unit={item.unit} freq={item.frequency}"
            )
            if len(samples) >= 10:
                break

    for line in samples:
        print(line)

    runtime = time.monotonic() - started
    if dry_run:
        print(
            f"turcat (dry-run): {len(fetched)} sectors ok, {len(failures)} failed; "
            f"indicators={total_indicators} values={total_values} "
            f"unparsed={total_unparsed} runtime={runtime:.1f}s"
        )
        _print_failures(failures)
        return 0

    assert session is not None
    institution = upsert_institution(
        session, connector.institution_code, connector.institution_name
    )
    session.commit()
    inserted = unchanged = points = series_count = 0
    for code in codes:
        fetch = fetched.get(code)
        if fetch is None:
            continue
        meta = connector.dataset_meta(code)
        outcome, dataset = upsert_dataset(session, institution.id, meta)
        result = ingest_sector(session, connector, dataset, fetch)
        session.commit()
        inserted += result.inserted
        unchanged += result.unchanged
        points += result.points
        series_count += result.series
        print(
            f"{code}: {outcome} series={result.series} inserted={result.inserted} "
            f"unchanged={result.unchanged} points={result.points} skipped={result.skipped}"
        )
    print(
        f"turcat: {len(fetched)} sectors, series={series_count} inserted={inserted} "
        f"unchanged={unchanged} points={points} runtime={time.monotonic() - started:.1f}s"
    )
    _print_failures(failures)
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


def _is_cip_command(args: argparse.Namespace) -> bool:
    if args.command == "cip-catalog":
        return True
    if args.command != "fetch":
        return False
    dataset = getattr(args, "dataset", None) or ""
    series = getattr(args, "series", None) or ""
    return dataset.startswith("CIP_") or series.startswith("CIP_")


def _run_cip(args: argparse.Namespace, dry_run: bool) -> int:
    if dry_run:
        connector = CipConnector(client=CipClient(store=None))
    else:
        connector = CipConnector()
    try:
        if args.command == "cip-catalog":
            if dry_run:
                return _cmd_cip_catalog(None, connector, args)
            from app.db.session import SessionLocal

            with SessionLocal() as session:
                return _cmd_cip_catalog(session, connector, args)
        if dry_run:
            return _dry_run_fetch(connector, args)
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            return _cmd_fetch(session, connector, args)
    finally:
        connector.close()


def _unexpected(key: str, exc: Exception) -> tuple[str, ConnectorError]:
    """Wrap an unexpected exception as a ``format_changed`` failure row."""
    return (key, ConnectorError(FORMAT_CHANGED, f"{type(exc).__name__}: {exc}"))


def _is_bi_command(args: argparse.Namespace) -> bool:
    if args.command in ("bi-catalog", "bi-headlines"):
        return True
    if args.command != "fetch":
        return False
    dataset = getattr(args, "dataset", None) or ""
    series = getattr(args, "series", None) or ""
    return dataset.startswith("TUIK_BI_") or series.startswith("TUIK_BI_")


def _build_bi_connector(dry_run: bool) -> BiTradeConnector:
    """Create the bi.tuik connector; a dry run never touches MinIO."""
    if dry_run:
        return BiTradeConnector(client=BiTradeClient(store=None))
    return BiTradeConnector()


def _bi_systems(args: argparse.Namespace) -> list:
    if getattr(args, "system", None):
        return [get_system(args.system)]
    return all_systems()


def _cmd_bi_catalog(
    session: Session | None, connector: BiTradeConnector, args: argparse.Namespace
) -> int:
    started = time.monotonic()
    dry_run = session is None
    institution_id: int | None = None
    if not dry_run:
        assert session is not None
        institution = upsert_institution(
            session, connector.institution_code, connector.institution_name
        )
        session.commit()
        institution_id = institution.id

    ok = failed = dimensions = codes = 0
    failures: list[tuple[str, ConnectorError]] = []
    for system in _bi_systems(args):
        try:
            meta = connector.dataset_meta(system)
        except ConnectorError as exc:
            if session is not None:
                session.rollback()
            failures.append((system.code, exc))
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one system must not abort the other
            if session is not None:
                session.rollback()
            failures.append(_unexpected(system.code, exc))
            failed += 1
            continue
        dimensions += len(meta.dimensions)
        codes += sum(len(dimension.codes) for dimension in meta.dimensions)
        if dry_run:
            _print_dimensions(system.code, meta)
        else:
            assert session is not None and institution_id is not None
            outcome, _ = upsert_dataset(session, institution_id, meta)
            session.commit()
            print(f"{system.code}: {outcome}")
        ok += 1

    runtime = time.monotonic() - started
    mode = " (dry-run)" if dry_run else ""
    print(f"bi-catalog{mode}: {ok} datasets ok, {failed} failed, runtime={runtime:.1f}s")
    print(f"dimensions={dimensions} codes={codes}")
    _print_failures(failures)
    return 0


def _record_headline(session: Session, dataset: Dataset, series) -> int:
    """Write one headline series (created on first use) and widen its coverage."""
    row = ensure_series(session, dataset, series.codes)
    raw_key = series.raw_object_keys[0] if series.raw_object_keys else None
    result = record_observations(
        session, row.id, series.points, fetched_at=datetime.now(UTC), raw_object_key=raw_key
    )
    periods = [period for period, _ in series.points]
    if periods:
        first, last = min(periods), max(periods)
        if row.coverage_start is None or first < row.coverage_start:
            row.coverage_start = first
        if row.coverage_end is None or last > row.coverage_end:
            row.coverage_end = last
    session.flush()
    return result.inserted


def _cmd_bi_headlines(
    session: Session | None, connector: BiTradeConnector, args: argparse.Namespace
) -> int:
    started = time.monotonic()
    dry_run = session is None
    only = getattr(args, "only", None)
    if only is not None and only not in HEADLINE_BY_NAME:
        print(
            f"bi-headlines: unknown --only {only!r}; choose from {sorted(HEADLINE_BY_NAME)}",
            file=sys.stderr,
        )
        return 1
    names = [only] if only else [spec.name for spec in HEADLINES]

    institution_id: int | None = None
    if not dry_run:
        assert session is not None
        institution = upsert_institution(
            session, connector.institution_code, connector.institution_name
        )
        session.commit()
        institution_id = institution.id

    total_series = total_points = total_inserted = 0
    failures: list[tuple[str, ConnectorError]] = []
    for system in _bi_systems(args):
        dataset: Dataset | None = None
        if not dry_run:
            assert session is not None and institution_id is not None
            meta = connector.dataset_meta(system)
            _, dataset = upsert_dataset(session, institution_id, meta)
            session.commit()
        for name in names:
            try:
                series_list = connector.headline_series(system, only=[name])
            except ConnectorError as exc:
                if session is not None:
                    session.rollback()
                failures.append((f"{system.code}:{name}", exc))
                continue
            except Exception as exc:  # noqa: BLE001 - one headline must not abort the rest
                if session is not None:
                    session.rollback()
                failures.append(_unexpected(f"{system.code}:{name}", exc))
                continue
            points = sum(len(series.points) for series in series_list)
            total_series += len(series_list)
            total_points += points
            if dry_run:
                print(f"{system.code} {name}: series={len(series_list)} points={points}")
                continue
            assert session is not None and dataset is not None
            try:
                inserted = 0
                for series in series_list:
                    inserted += _record_headline(session, dataset, series)
                session.commit()
            except Exception as exc:  # noqa: BLE001 - one headline must not abort the rest
                session.rollback()
                failures.append(_unexpected(f"{system.code}:{name}", exc))
                continue
            total_inserted += inserted
            print(
                f"{system.code} {name}: series={len(series_list)} points={points} "
                f"inserted={inserted}"
            )

    runtime = time.monotonic() - started
    mode = " (dry-run)" if dry_run else ""
    print(
        f"bi-headlines{mode}: series={total_series} points={total_points} "
        f"inserted={total_inserted} runtime={runtime:.1f}s"
    )
    _print_failures(failures)
    return 0


def _dry_run_bi_fetch(connector: BiTradeConnector, args: argparse.Namespace) -> int:
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
        system = get_system(dataset_code)
        order = dimension_codes(system)
        parts = key.split(".")
        if len(parts) != len(order):
            _print_error("fetch", ValueError(f"expected {len(order)} codes {order}, got {key!r}"))
            return 1
        codes = dict(zip(order, parts, strict=True))
    elif args.dataset:
        dataset_code = args.dataset
        system = get_system(dataset_code)
        codes = _parse_codes(args.code)
    else:
        print(
            "fetch: pass --series EXTERNAL_CODE or --dataset TUIK_BI_GTS --code DIM=CODE",
            file=sys.stderr,
        )
        return 1

    try:
        codes = complete_codes(system, codes)
        validate_codes(system, codes)
        result = connector.fetch_series(dataset_code, codes, start=start)
    except (ConnectorError, SeriesDefinitionError) as exc:
        _print_error("fetch", exc)
        return 1
    print(
        f"fetch {dataset_code}: points={len(result.points)} "
        f"channel={result.channel} (dry-run, nothing written)"
    )
    if result.points:
        print(f"periods: {result.points[0][0].isoformat()}..{result.points[-1][0].isoformat()}")
        last_period, last_value = result.points[-1]
        print(f"last: {last_period.isoformat()}={last_value}")
        july = next((value for period, value in result.points if period == date(2026, 7, 1)), None)
        if july is not None:
            print(f"2026-07: {july}")
    return 0


def _cmd_bi_fetch(session: Session, connector: BiTradeConnector, args: argparse.Namespace) -> int:
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
                "fetch: pass --series EXTERNAL_CODE or --dataset TUIK_BI_GTS --code DIM=CODE",
                file=sys.stderr,
            )
            return 1
        system = get_system(dataset.external_code)
        codes = complete_codes(system, codes)
        validate_codes(system, codes)
    except (ValueError, SeriesDefinitionError) as exc:
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


def _run_bi(args: argparse.Namespace, dry_run: bool) -> int:
    connector = _build_bi_connector(dry_run)
    try:
        if args.command == "bi-catalog":
            if dry_run:
                return _cmd_bi_catalog(None, connector, args)
            from app.db.session import SessionLocal

            with SessionLocal() as session:
                return _cmd_bi_catalog(session, connector, args)
        if args.command == "bi-headlines":
            if dry_run:
                return _cmd_bi_headlines(None, connector, args)
            from app.db.session import SessionLocal

            with SessionLocal() as session:
                return _cmd_bi_headlines(session, connector, args)
        if dry_run:
            return _dry_run_bi_fetch(connector, args)
        from app.db.session import SessionLocal

        with SessionLocal() as session:
            return _cmd_bi_fetch(session, connector, args)
    finally:
        connector.close()


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    dry_run = bool(getattr(args, "dry_run", False))
    if _is_bi_command(args):
        return _run_bi(args, dry_run)
    if args.command in ("siniflama", "siniflama-link-dimensions"):
        return _run_siniflama(args, dry_run)
    if _is_cip_command(args):
        return _run_cip(args, dry_run)

    connector = _build_connector(args.command, dry_run, _known_for(args.command, dry_run))
    try:
        if dry_run and args.command == "catalog":
            return _cmd_catalog(None, connector, args)
        if dry_run and args.command == "fetch":
            return _dry_run_fetch(connector, args)
        if dry_run and args.command == "turcat":
            return _cmd_turcat(None, connector, args)

        from app.db.session import SessionLocal

        handlers = {
            "catalog": _cmd_catalog,
            "fetch": _cmd_fetch,
            "find": _cmd_find,
            "turcat": _cmd_turcat,
        }
        with SessionLocal() as session:
            return handlers[args.command](session, connector, args)
    finally:
        connector.close()


def _build_connector(
    command: str, dry_run: bool, known: list[DataflowInfo] | None = None
) -> TuikConnector | TurcatConnector:
    """Create the connector for ``command``; dry runs never touch MinIO."""
    if command == "turcat":
        if dry_run:
            return TurcatConnector(client=TurcatClient(store=None))
        return TurcatConnector()
    try:
        nsiws = build_nsiws_client(store=None if dry_run else MinioObjectStore())
    except Exception:  # noqa: BLE001 - the backup must never stop the primary CLI
        logger.warning("tuik nsiws backup unavailable", exc_info=True)
        nsiws = None
    if dry_run:
        return TuikConnector(
            client=Databrowser2Client(store=None), nsiws=nsiws, known_dataflows=known or ()
        )
    return TuikConnector(nsiws=nsiws, known_dataflows=known or ())


if __name__ == "__main__":
    raise SystemExit(main())
