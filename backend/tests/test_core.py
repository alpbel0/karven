"""Unit tests for the core registry, rounding comparison and CLI parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.connectors.tcmb.connector import SOURCES
from app.core.__main__ import _cmd_calendar_sync, build_parser
from app.core.loader import rounds_equal, select_core
from app.core.registry import (
    CORE_SERIES,
    GDP_DATASET,
    GDP_SERIE_SUFFIXES,
    GDP_SOURCE,
    TURCAT_GDP_LINKS,
)


def test_registry_has_exactly_15_unique_entries() -> None:
    assert len(CORE_SERIES) == 15
    external_codes = [core.external_code for core in CORE_SERIES]
    assert len(set(external_codes)) == 15
    assert len({(core.source, core.dataset_code, core.serie_code) for core in CORE_SERIES}) == 15


def test_every_registry_source_exists() -> None:
    for core in CORE_SERIES:
        assert core.source in SOURCES


def test_registry_never_contains_b1g() -> None:
    assert all(not core.serie_code.endswith(".B1G") for core in CORE_SERIES)
    assert "TP.GSYIH040.IFK.B1G" not in {core.serie_code for core in CORE_SERIES}


def test_registry_membership() -> None:
    codes = {core.external_code for core in CORE_SERIES}
    assert "bie_dkefkytl:TP.DK.USD.A.EF.YTL" in codes
    assert "bie_cli2:TP.CLI2.A01" in codes
    gdp = [core for core in CORE_SERIES if core.source == GDP_SOURCE]
    assert len(gdp) == 13
    assert [core.serie_code for core in gdp] == [
        f"TP.GSYIH040.IFK.{suffix}" for suffix in GDP_SERIE_SUFFIXES
    ]
    assert all(core.dataset_code == GDP_DATASET for core in gdp)


def test_external_code_property() -> None:
    core = CORE_SERIES[0]
    assert core.external_code == f"{core.dataset_code}:{core.serie_code}"


def test_turcat_links_map_indicators_to_gdp_series() -> None:
    assert len(TURCAT_GDP_LINKS) == 13
    assert [indicator for indicator, _ in TURCAT_GDP_LINKS] == [str(n) for n in range(2, 15)]
    expected = [
        core.serie_code
        for core in CORE_SERIES
        if core.dataset_code == GDP_DATASET
    ]
    assert [serie_code for _, serie_code in TURCAT_GDP_LINKS] == expected


def test_rounds_equal_matches_evds_rounded_to_integer() -> None:
    assert rounds_equal(Decimal("19869747341.4119"), Decimal("19869747341")) is True
    assert rounds_equal(Decimal("17098138220.9303"), Decimal("17098138221")) is True
    assert rounds_equal(Decimal("19869747341.4119"), Decimal("19869747342")) is False
    assert rounds_equal(None, Decimal("1")) is False
    assert rounds_equal(Decimal("1"), None) is False


def test_select_core_rejects_unknown_external_code() -> None:
    assert len(select_core(None)) == 15
    assert select_core("bie_cli2:TP.CLI2.A01")[0].serie_code == "TP.CLI2.A01"
    with pytest.raises(ValueError):
        select_core("bie_cli2:TP.CLI2.A02")


def test_build_parser_load() -> None:
    args = build_parser().parse_args(["load"])
    assert args.command == "load"
    assert args.only is None
    assert args.dry_run is False
    args = build_parser().parse_args(["load", "--only", "bie_cli2:TP.CLI2.A01", "--dry-run"])
    assert args.only == "bie_cli2:TP.CLI2.A01"
    assert args.dry_run is True


def test_build_parser_link_turcat_and_status() -> None:
    args = build_parser().parse_args(["link-turcat"])
    assert args.command == "link-turcat"
    assert args.dry_run is False
    assert build_parser().parse_args(["link-turcat", "--dry-run"]).dry_run is True
    assert build_parser().parse_args(["status"]).command == "status"


def test_build_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- calendar-sync CLI transaction ------------------------------------------


class _CalResp:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _EmptyCalendarClient:
    """A calendar client whose sources return no rows (so the session is untouched)."""

    def get_calendar(self, year: int, month: int) -> _CalResp:
        return _CalResp([])

    def fetch(self, year: int) -> _CalResp:
        return _CalResp({})

    def close(self) -> None:
        return None


class _BoomCalendarClient(_EmptyCalendarClient):
    def get_calendar(self, year: int, month: int) -> _CalResp:
        raise RuntimeError("boom")


class _RecordingSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_calendar_sync_command_commits_exactly_once() -> None:
    session = _RecordingSession()
    args = build_parser().parse_args(["calendar-sync"])
    code = _cmd_calendar_sync(
        session,
        args,
        now=datetime(2026, 10, 3, 12, tzinfo=UTC),
        evds=_EmptyCalendarClient(),
        tuik=_EmptyCalendarClient(),
    )
    assert code == 0
    assert session.commits == 1
    assert session.rollbacks == 0


def test_calendar_sync_command_rolls_back_and_reraises() -> None:
    session = _RecordingSession()
    args = build_parser().parse_args(["calendar-sync"])
    with pytest.raises(RuntimeError):
        _cmd_calendar_sync(
            session,
            args,
            now=datetime(2026, 10, 3, 12, tzinfo=UTC),
            evds=_BoomCalendarClient(),
            tuik=_EmptyCalendarClient(),
        )
    assert session.commits == 0
    assert session.rollbacks == 1
