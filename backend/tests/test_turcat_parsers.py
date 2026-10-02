"""Unit tests for the Turcat (IMF SDDS) pure parsers (fixtures, no network)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.connectors.tuik.turcat_parsers import (
    parse_sector,
    parse_turcat_payload,
    parse_turcat_period,
    parse_turcat_value,
    previous_period,
)
from app.data.periods import ANNUAL, DAILY, MONTHLY, QUARTERLY

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "turcat"

REEL = (FIXTURES / "reel.turcat.raw.json").read_bytes()
MALI = (FIXTURES / "mali.turcat.raw.json").read_bytes()
FINANS = (FIXTURES / "finans.turcat.raw.json").read_bytes()
DIS = (FIXTURES / "dis.turcat.raw.json").read_bytes()
NUFUS = (FIXTURES / "nufus.turcat.raw.json").read_bytes()


def test_parse_turcat_value_turkish_formats() -> None:
    assert parse_turcat_value("86 092") == Decimal("86092")
    assert parse_turcat_value("19 869 747 341") == Decimal("19869747341")
    assert parse_turcat_value("19.869.747.341") == Decimal("19869747341")
    assert parse_turcat_value("184.246,6") == Decimal("184246.6")
    assert parse_turcat_value("28 148 817 212,9") == Decimal("28148817212.9")
    assert parse_turcat_value("-87 788 839,0") == Decimal("-87788839.0")
    assert parse_turcat_value(" 844 054 369 ") == Decimal("844054369")
    assert parse_turcat_value("0,0") == Decimal("0.0")
    assert parse_turcat_value("0") == Decimal("0")


def test_parse_turcat_value_missing_markers() -> None:
    assert parse_turcat_value(None) is None
    assert parse_turcat_value("") is None
    assert parse_turcat_value("-") is None
    assert parse_turcat_value("..") is None


def test_parse_turcat_value_rejects_garbage() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_turcat_value("12,3,4")
    assert excinfo.value.kind == FORMAT_CHANGED


def test_parse_turcat_period_shapes() -> None:
    assert parse_turcat_period("2025") == (date(2025, 1, 1), ANNUAL)
    assert parse_turcat_period("Ağu/26") == (date(2026, 8, 1), MONTHLY)
    assert parse_turcat_period("Eyl/26") == (date(2026, 9, 1), MONTHLY)
    assert parse_turcat_period("Tem/26\xa0") == (date(2026, 7, 1), MONTHLY)
    assert parse_turcat_period("D2/26") == (date(2026, 4, 1), QUARTERLY)
    assert parse_turcat_period("D1/25") == (date(2025, 1, 1), QUARTERLY)
    assert parse_turcat_period("04/Eyl/26") == (date(2026, 9, 4), DAILY)
    assert parse_turcat_period("18/Eyl/26") == (date(2026, 9, 18), DAILY)


@pytest.mark.parametrize("bad", ["13/2026", "D5/26", "XXX/26", "2026-09", "Oca"])
def test_parse_turcat_period_unknown_is_format_changed(bad: str) -> None:
    with pytest.raises(ConnectorError) as excinfo:
        parse_turcat_period(bad)
    assert excinfo.value.kind == FORMAT_CHANGED


def test_previous_period_steps_back_one_period() -> None:
    assert previous_period(ANNUAL, date(2025, 1, 1)) == date(2024, 1, 1)
    assert previous_period(QUARTERLY, date(2026, 4, 1)) == date(2026, 1, 1)
    assert previous_period(QUARTERLY, date(2026, 1, 1)) == date(2025, 10, 1)
    assert previous_period(MONTHLY, date(2026, 1, 1)) == date(2025, 12, 1)
    # Day-level snapshots have no knowable previous date: never invent one.
    assert previous_period(DAILY, date(2026, 9, 18)) is None


def test_parse_turcat_payload_strips_bom() -> None:
    rows = parse_turcat_payload(b"\xef\xbb\xbf" + NUFUS)
    assert rows and rows[0]["ID"] == 251


def test_parse_sector_nufus_single_annual_indicator() -> None:
    parsed = parse_sector(NUFUS, "TURCAT_NUFUS")
    assert parsed.page == "Nüfus"
    assert parsed.page_id == 5
    assert parsed.groups == []
    assert len(parsed.indicators) == 1
    (indicator,) = parsed.indicators
    assert indicator.code == "251"
    assert indicator.name == "Nüfus"
    assert indicator.unit == "Bin kişi"
    assert indicator.frequency == ANNUAL
    assert indicator.parent_code is None
    assert indicator.period == date(2025, 1, 1)
    assert indicator.previous_period == date(2024, 1, 1)
    assert indicator.latest_value == Decimal("86092")
    assert indicator.previous_value == Decimal("85665")
    assert indicator.meta_url.startswith("https://dsbb.imf.org/sdds/dqaf-base/country/TUR/")
    assert parsed.errors == []


def test_parse_sector_reel_has_group_parent_link() -> None:
    parsed = parse_sector(REEL, "TURCAT_REEL")
    assert len(parsed.groups) == 1
    group = parsed.groups[0]
    assert group.code == "1"
    assert group.name == "Ulusal Hesaplar"
    first = next(item for item in parsed.indicators if item.code == "2")
    assert first.parent_code == "1"
    assert first.period == date(2026, 4, 1)
    assert first.latest_value == Decimal("19869747341")
    assert first.previous_period == date(2026, 1, 1)
    assert parsed.errors == []


def test_parse_sector_reports_value_without_period() -> None:
    mali = parse_sector(MALI, "TURCAT_MALI")
    dis = parse_sector(DIS, "TURCAT_DIS")
    assert len(mali.errors) == 4
    assert all("value without a period" in error for error in mali.errors)
    assert len(dis.errors) == 25
    # The erroring rows keep their catalog code but carry no period.
    erroring = [item for item in mali.indicators if item.period is None]
    assert erroring and all(item.latest_value is not None for item in erroring)


def test_parse_sector_finans_counts() -> None:
    parsed = parse_sector(FINANS, "TURCAT_FINANS")
    assert len(parsed.indicators) == 9
    assert len(parsed.groups) == 8
    values = [item for item in parsed.indicators if item.latest_value is not None]
    assert len(values) == 8
