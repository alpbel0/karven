"""Unit tests for calendar parsing/filtering and the core config defaults.

Fixtures are TRIMMED real rows copied from the live October-2026 EVDS and 2026
TÜİK calendars (see ``tests/fixtures/calendar``); a couple of synthetic rows add
the ``veriGrubuKodu: null`` and "Periyodik güncellenir" shapes the real sources
also produce.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.core.calendar import (
    CALENDAR_KEYS,
    EVDS3,
    TUIK,
    TUIK_GDP_KEY,
    evds_months,
    parse_evds_calendar,
    parse_tuik_calendar,
    relevant_entries,
)
from app.core.registry import CORE_SERIES

FIXTURES = Path(__file__).parent / "fixtures" / "calendar"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- registry mapping -------------------------------------------------------


def test_calendar_keys_cover_every_core_series() -> None:
    assert set(CALENDAR_KEYS) == {core.external_code for core in CORE_SERIES}
    assert CALENDAR_KEYS["bie_cli2:TP.CLI2.A01"] == (EVDS3, "bie_cli2")
    gdp = CALENDAR_KEYS["bie_gsyhuretcar:TP.GSYIH040.IFK.B1GQ"]
    assert gdp == (TUIK, TUIK_GDP_KEY)
    assert CALENDAR_KEYS["bie_dkefkytl:TP.DK.USD.A.EF.YTL"] is None


def test_only_usd_is_calendar_less() -> None:
    missing = [code for code, pair in CALENDAR_KEYS.items() if pair is None]
    assert missing == ["bie_dkefkytl:TP.DK.USD.A.EF.YTL"]


# --- EVDS parsing -----------------------------------------------------------


def test_evds_parses_real_cli2_row() -> None:
    entries = parse_evds_calendar(_load("evds-2026-10.trimmed.json"))
    cli2 = [entry for entry in entries if entry.key == "bie_cli2"]
    assert {entry.expected_on for entry in cli2} == {date(2026, 10, 30), date(2026, 10, 20)}
    release = next(entry for entry in cli2 if entry.expected_on == date(2026, 10, 30))
    assert release.source == EVDS3
    assert release.expected_at == "2026-10-30 23:00"
    assert release.period_label == "Eylül 2026"
    assert release.attributes["yayinBilgi"]["yayimAdi"].startswith("Bileşik")


def test_evds_skips_null_datagroup_but_keeps_periodic() -> None:
    entries = parse_evds_calendar(_load("evds-2026-10.trimmed.json"))
    keys = [entry.key for entry in entries]
    # The synthetic null-group row is dropped; the periodic bie_cli2 row is kept.
    assert "bie_bkea" in keys
    assert keys.count("bie_cli2") == 2


def test_evds_relevant_keeps_only_core_keys() -> None:
    entries = relevant_entries(parse_evds_calendar(_load("evds-2026-10.trimmed.json")))
    assert {entry.key for entry in entries} == {"bie_cli2"}


def test_evds_rejects_non_array() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_evds_calendar({"not": "a list"})
    assert excinfo.value.kind == FORMAT_CHANGED


# --- TÜİK parsing -----------------------------------------------------------


def test_tuik_keeps_only_gdp_rows_from_tuik() -> None:
    entries = parse_tuik_calendar(_load("tuik-2026.trimmed.json"))
    assert all(entry.source == TUIK for entry in entries)
    assert all(entry.key == TUIK_GDP_KEY for entry in entries)
    # The other-agency GDP row is filtered out: only the two TÜİK GDP rows remain.
    assert len(entries) == 2
    dates = {entry.expected_on for entry in entries}
    assert dates == {date(2026, 8, 31), date(2026, 12, 1)}


def test_tuik_uses_both_published_and_upcoming_lists() -> None:
    entries = parse_tuik_calendar(_load("tuik-2026.trimmed.json"))
    upcoming = next(entry for entry in entries if entry.expected_on == date(2026, 12, 1))
    assert upcoming.attributes["id"] == 0
    assert upcoming.period_label == "III. Çeyrek: Temmuz-Eylül 2026"


def test_tuik_rejects_non_object() -> None:
    with pytest.raises(ConnectorError):
        parse_tuik_calendar([{"not": "object"}])


# --- month window -----------------------------------------------------------


def test_evds_months_spans_previous_to_plus_three() -> None:
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    assert evds_months(now) == [
        (2026, 9),
        (2026, 10),
        (2026, 11),
        (2026, 12),
        (2027, 1),
    ]


def test_evds_months_handles_year_boundary() -> None:
    now = datetime(2026, 1, 15, tzinfo=UTC)
    assert evds_months(now) == [
        (2025, 12),
        (2026, 1),
        (2026, 2),
        (2026, 3),
        (2026, 4),
    ]


# --- config defaults --------------------------------------------------------


def test_core_refresh_config_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.core_grace_days == 2
    assert settings.core_retry_hours == 2
    assert settings.core_daily_after == "16:30"
    assert settings.core_daily_stale_days == 10
    assert settings.core_failure_threshold == 3
