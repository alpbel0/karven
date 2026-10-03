"""CLI: ``python -m app.core``.

Commands:

- ``load [--only EXTERNAL_CODE] [--dry-run]`` — fetch and ingest the 15 core
  series from ``2000-01-01``, flag each ``is_core``, and report failures. One
  transaction per series; ``--dry-run`` fetches and parses but writes nothing.
- ``link-turcat [--dry-run]`` — verify each Turcat GDP row against its EVDS
  series and store the accepted ``same_series`` link (Turcat values must equal
  the EVDS value rounded to an integer for every Turcat period).
- ``status`` — one line per core series plus the Turcat link count.
- ``calendar-sync [--dry-run]`` — fetch the EVDS3 and TÜİK release calendars and
  upsert the core-relevant rows; ``--dry-run`` fetches, parses and counts but
  writes nothing.
- ``refresh [--only EXTERNAL_CODE] [--force] [--dry-run]`` — refresh every core
  series whose release calendar says it is due. Exits non-zero when a refresh
  failed.
- ``alerts [--all]`` — list the open data alerts (or every alert with ``--all``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.connectors.base import MinioObjectStore
from app.connectors.tcmb.connector import TcmbConnector
from app.core.alerts import list_alerts
from app.core.calendar import (
    evds_months,
    parse_evds_calendar,
    parse_tuik_calendar,
    relevant_entries,
    sync_calendar,
)
from app.core.clients import (
    build_connector,
    build_evds_calendar_client,
    build_tuik_calendar_client,
)
from app.core.loader import (
    ConnectorFor,
    core_status,
    link_turcat,
    load_core,
    select_core,
    turcat_gdp_link_count,
)
from app.core.refresh import ConnectorFor as RefreshConnectorFor
from app.core.refresh import refresh_core


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (pure, importable without a database)."""
    parser = argparse.ArgumentParser(
        prog="app.core",
        description="Core-series loader and Turcat GDP linker (Task 1.4b).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="fetch and flag the 15 core series")
    load.add_argument(
        "--only",
        default=None,
        metavar="EXTERNAL_CODE",
        help="load only this core series (e.g. bie_cli2:TP.CLI2.A01)",
    )
    load.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and parse but write nothing (no DB, no MinIO)",
    )

    link = subparsers.add_parser(
        "link-turcat",
        help="verify and accept the Turcat -> EVDS GDP links",
    )
    link.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="verify every pair but write no catalog_links row",
    )

    subparsers.add_parser("status", help="print core series and Turcat link status")

    calendar = subparsers.add_parser(
        "calendar-sync", help="fetch and store the core-relevant release calendar rows"
    )
    calendar.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="fetch and count but write nothing (no DB, no MinIO)",
    )

    refresh = subparsers.add_parser(
        "refresh", help="refresh every core series whose release calendar says it is due"
    )
    refresh.add_argument(
        "--only",
        default=None,
        metavar="EXTERNAL_CODE",
        help="refresh only this core series (e.g. bie_cli2:TP.CLI2.A01)",
    )
    refresh.add_argument(
        "--force",
        action="store_true",
        help="refresh every selected series regardless of its release date",
    )
    refresh.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="report the decisions only; write nothing",
    )

    alerts = subparsers.add_parser("alerts", help="list data alerts")
    alerts.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="list resolved alerts too (default: open alerts only)",
    )
    return parser


def _build_connector(source: str, *, dry_run: bool) -> TcmbConnector:
    """Create the connector for one source; a dry run never touches MinIO."""
    return build_connector(source, store=None if dry_run else MinioObjectStore())


def _cmd_load(session, connector_for: ConnectorFor, args: argparse.Namespace) -> int:
    try:
        select_core(args.only)
    except ValueError as exc:
        print(f"load: {exc}", file=sys.stderr)
        return 1

    loads, failures = load_core(
        session, connector_for, only=args.only, dry_run=bool(args.dry_run)
    )
    mode = " (dry-run)" if args.dry_run else ""
    for item in loads:
        print(
            f"load{mode} {item.external_code}: inserted={item.inserted} "
            f"unchanged={item.unchanged} points={item.point_count}"
        )
    for failure in failures:
        print(f"load: {failure.external_code} failed: {failure.message}", file=sys.stderr)
    if failures:
        print(f"load: {len(failures)} series failed", file=sys.stderr)
        return 1
    print(f"load{mode}: {len(loads)} series ok")
    return 0


def _cmd_link_turcat(session, args: argparse.Namespace) -> int:
    try:
        links, failures = link_turcat(session, dry_run=bool(args.dry_run))
    except LookupError as exc:
        print(f"link-turcat: {exc}", file=sys.stderr)
        return 1

    mode = " (dry-run)" if args.dry_run else ""
    verb = "verified" if args.dry_run else "linked"
    for item in links:
        periods = ", ".join(period.isoformat() for period in item.periods)
        print(f"link-turcat{mode} {item.indicator} -> {item.serie_code}: {verb} for {periods}")
    for failure in failures:
        print(
            f"link-turcat: {failure.indicator} -> {failure.serie_code} refused: "
            f"{failure.message}",
            file=sys.stderr,
        )
    if failures:
        print(f"link-turcat: {len(failures)} pair(s) refused", file=sys.stderr)
        return 1
    outcome = "verified" if args.dry_run else "accepted"
    print(f"link-turcat{mode}: {len(links)} link(s) {outcome}")
    return 0


def _cmd_status(session) -> int:
    for item in core_status(session):
        latest = item.latest_period.isoformat() if item.latest_period else "-"
        print(
            f"{item.external_code} is_core={item.is_core} "
            f"observations={item.observation_count} latest={latest}"
        )
    print(f"status: turcat links={turcat_gdp_link_count(session)}")
    return 0


def _cmd_calendar_sync(
    session,
    args: argparse.Namespace,
    *,
    now: datetime,
    evds: Any | None = None,
    tuik: Any | None = None,
) -> int:
    """Fetch and store the release calendar; the CLI owns the transaction.

    ``evds``/``tuik`` are injectable for tests and default to the real clients.
    A successful non-dry-run sync is committed here; any failure rolls back and
    propagates so the process exits non-zero.
    """
    dry_run = bool(args.dry_run)
    if evds is None or tuik is None:
        store = None if dry_run else MinioObjectStore()
        if evds is None:
            evds = build_evds_calendar_client(store=store)
        if tuik is None:
            tuik = build_tuik_calendar_client(store=store)
    try:
        if dry_run:
            entries = []
            for year, month in evds_months(now):
                entries.extend(
                    relevant_entries(parse_evds_calendar(evds.get_calendar(year, month).json()))
                )
            for year in (now.year, now.year + 1):
                entries.extend(relevant_entries(parse_tuik_calendar(tuik.fetch(year).json())))
            unique = {(entry.source, entry.key, entry.expected_on) for entry in entries}
            print(f"calendar-sync (dry-run): {len(unique)} core-relevant rows")
            return 0
        result = sync_calendar(
            session, now=now, evds_client=evds, tuik_fetch=tuik.fetch
        )
        session.commit()
        print(
            f"calendar-sync: inserted={result.inserted} updated={result.updated} "
            f"unchanged={result.unchanged} sources={','.join(result.sources)}"
        )
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        evds.close()
        tuik.close()


def _cmd_refresh(connector_for: RefreshConnectorFor, args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    try:
        report = refresh_core(
            SessionLocal,
            connector_for,
            only=args.only,
            force=bool(args.force),
            dry_run=bool(args.dry_run),
        )
    except ValueError as exc:
        print(f"refresh: {exc}", file=sys.stderr)
        return 1

    mode = " (dry-run)" if args.dry_run else ""
    for item in report.series:
        opened = ",".join(item.alerts_opened) or "-"
        resolved = ",".join(item.alerts_resolved) or "-"
        print(
            f"refresh{mode} {item.external_code}: action={item.action} outcome={item.outcome} "
            f"due={item.due} reason={item.reason} inserted={item.inserted} "
            f"opened={opened} resolved={resolved}"
        )
        if item.error:
            print(f"refresh: {item.external_code} error: {item.error}", file=sys.stderr)
    if report.failed:
        print(f"refresh{mode}: {report.fetched} fetched, failures recorded", file=sys.stderr)
        return 1
    print(f"refresh{mode}: {report.fetched} fetched")
    return 0


def _cmd_alerts(session, args: argparse.Namespace) -> int:
    statuses = None if args.show_all else {"open"}
    rows = list_alerts(session, statuses=statuses)
    for alert in rows:
        seen = alert.last_seen_at.isoformat() if alert.last_seen_at else "-"
        print(
            f"alerts {alert.status} {alert.kind} scope={alert.scope} "
            f"opened={alert.opened_at.isoformat()} last_seen={seen} message={alert.message}"
        )
    print(f"alerts: {len(rows)} row(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.db.session import SessionLocal

    now = datetime.now(UTC)
    if args.command == "status":
        with SessionLocal() as session:
            return _cmd_status(session)
    if args.command == "alerts":
        with SessionLocal() as session:
            return _cmd_alerts(session, args)
    if args.command == "calendar-sync":
        with SessionLocal() as session:
            return _cmd_calendar_sync(session, args, now=now)

    dry_run = bool(getattr(args, "dry_run", False))
    connectors: dict[str, TcmbConnector] = {}

    def connector_for(source: str) -> TcmbConnector:
        connector = connectors.get(source)
        if connector is None:
            connector = _build_connector(source, dry_run=dry_run)
            connectors[source] = connector
        return connector

    try:
        if args.command == "refresh":
            return _cmd_refresh(connector_for, args)
        with SessionLocal() as session:
            if args.command == "load":
                return _cmd_load(session, connector_for, args)
            return _cmd_link_turcat(session, args)
    finally:
        for connector in connectors.values():
            connector.close()


if __name__ == "__main__":
    raise SystemExit(main())
