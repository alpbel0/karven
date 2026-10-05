"""Pure parsers for the TÜİK Turcat (IMF SDDS national summary data page).

Turcat is a small, token-free JSON endpoint per sector (Reel/Mali/Finans/Dis/
Nufus). Each row is one indicator with its latest and previous values, a period
string, a unit and an IMF DSBB metadata link. Nothing here touches the network
or the database: periods, values and rows are parsed in isolation so they are
fixture-testable.

Period strings are exact and must never be guessed. The source uses four shapes:

- ``2025``            -> annual
- ``Ağu/26``          -> monthly (Turkish month abbreviation)
- ``D2/26``           -> quarterly (period 2 of 2026 -> Q2)
- ``04/Eyl/26``       -> day/month/year (a daily-looking snapshot date)

An unknown shape raises :class:`ConnectorError` with kind ``format_changed``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.data.periods import ANNUAL, DAILY, MONTHLY, QUARTERLY

# Sector endpoints, in display order. ``key`` is the Turcat URL suffix.
SECTORS: tuple[tuple[str, str, str, int], ...] = (
    ("TURCAT_REEL", "Reel", "Reel Sektör", 1),
    ("TURCAT_MALI", "Mali", "Mali Sektör", 2),
    ("TURCAT_FINANS", "Finans", "Finans Sektörü", 3),
    ("TURCAT_DIS", "Dis", "Dış Sektör", 4),
    ("TURCAT_NUFUS", "Nufus", "Nüfus", 5),
)

SECTOR_BY_EXTERNAL_CODE = {code: (key, title, page_id) for code, key, title, page_id in SECTORS}
SECTOR_BY_KEY = {key: (code, title, page_id) for code, key, title, page_id in SECTORS}

_INDICATOR_DIMENSION = "INDICATOR"

_TR_MONTHS = {
    "OCA": 1,
    "SUB": 2,
    "MAR": 3,
    "NIS": 4,
    "MAY": 5,
    "HAZ": 6,
    "TEM": 7,
    "AGU": 8,
    "EYL": 9,
    "EKI": 10,
    "KAS": 11,
    "ARA": 12,
}

_TR_ASCII = str.maketrans(
    {
        "Ğ": "G",
        "Ü": "U",
        "Ş": "S",
        "İ": "I",
        "Ö": "O",
        "Ç": "C",
        "I": "I",
    }
)

_ANNUAL_RE = re.compile(r"^(\d{4})$")
_DAILY_RE = re.compile(r"^(\d{1,2})/([A-Z]{3})/(\d{2})$")
_MONTHLY_RE = re.compile(r"^([A-Z]{3})/(\d{2})$")
_PERIOD_RE = re.compile(r"^D([1-4])/(\d{2})$")

# Value markers that mean "no value" (Turkish formatting never uses a comma-only
# or dot-only convention, so the rules below are unambiguous for this source).
_MISSING_MARKERS = {"", "-", "--", ".", "..", "..."}


def _clean_period_text(raw: str) -> str:
    return raw.replace("\xa0", " ").strip()


def _ascii_upper(text: str) -> str:
    return _clean_period_text(text).upper().translate(_TR_ASCII)


def parse_turcat_period(text: str) -> tuple[date, str]:
    """Parse a Turcat period string into ``(period_start, frequency)``.

    ``D<n>/YY`` is a Turkish quarter (``D2/26`` -> Q2 2026) and ``DD/Mon/YY`` is
    a day-level snapshot date. Anything else raises ``format_changed``.
    """
    if not isinstance(text, str):
        raise ConnectorError(FORMAT_CHANGED, f"Turcat period must be a string, got {text!r}")
    value = _clean_period_text(text)
    upper = _ascii_upper(value)

    match = _DAILY_RE.fullmatch(upper)
    if match:
        month = _TR_MONTHS.get(match[2])
        if month is None:
            raise ConnectorError(FORMAT_CHANGED, f"unknown Turkish month in period {text!r}")
        return date(2000 + int(match[3]), month, int(match[1])), DAILY

    match = _MONTHLY_RE.fullmatch(upper)
    if match:
        month = _TR_MONTHS.get(match[1])
        if month is None:
            raise ConnectorError(FORMAT_CHANGED, f"unknown Turkish month in period {text!r}")
        return date(2000 + int(match[2]), month, 1), MONTHLY

    match = _PERIOD_RE.fullmatch(upper)
    if match:
        quarter = int(match[1])
        return date(2000 + int(match[2]), (quarter - 1) * 3 + 1, 1), QUARTERLY

    match = _ANNUAL_RE.fullmatch(upper)
    if match:
        return date(int(match[1]), 1, 1), ANNUAL

    raise ConnectorError(FORMAT_CHANGED, f"unrecognized Turcat period {text!r}")


def previous_period(frequency: str, period: date) -> date | None:
    """Return the period before ``period`` for the inferred frequency.

    The source only publishes the latest period. For annual/quarterly/monthly
    series the previous period is exactly one step back. Day-level snapshots
    (e.g. weekly reserves published with a date) have no knowable previous
    date, so ``None`` is returned and the previous value is NOT stored —
    never invent a period.
    """
    if frequency == ANNUAL:
        return date(period.year - 1, 1, 1)
    if frequency == QUARTERLY:
        month = period.month - 3
        year = period.year
        if month <= 0:
            month += 12
            year -= 1
        return date(year, month, 1)
    if frequency == MONTHLY:
        month = period.month - 1
        year = period.year
        if month == 0:
            month = 12
            year -= 1
        return date(year, month, 1)
    if frequency == DAILY:
        return None
    raise ConnectorError(FORMAT_CHANGED, f"unknown frequency {frequency!r}")


def parse_turcat_value(raw: object) -> Decimal | None:
    """Parse a Turcat value using explicit Turkish number rules.

    Turkish formatting: ``"86 092"`` (space thousands), ``"19.869.747.341"``
    (dot thousands), ``"184.246,6"`` (dot thousands + comma decimal), and a
    leading ``-`` for negatives. Empty markers become ``None``.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ConnectorError(FORMAT_CHANGED, f"boolean Turcat value {raw!r}")
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))
    text = str(raw).replace("\xa0", " ").strip()
    if text in _MISSING_MARKERS:
        return None
    negative = text.startswith("-")
    if negative:
        text = text[1:].strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    else:
        # A lone separator can only be a thousands separator in this source.
        text = text.replace(".", "").replace(" ", "")
    text = text.replace(" ", "")
    if not text or text == ".":
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ConnectorError(FORMAT_CHANGED, f"invalid Turcat value {raw!r}") from exc
    return -value if negative else value


def parse_turcat_payload(payload: bytes | str) -> list[dict[str, Any]]:
    """Decode a Turcat body (UTF-8, optional BOM) into a list of row objects."""
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConnectorError(FORMAT_CHANGED, "Turcat body is not UTF-8") from exc
    else:
        text = payload
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ConnectorError(FORMAT_CHANGED, "Turcat body is not valid JSON") from exc
    if not isinstance(data, list):
        raise ConnectorError(FORMAT_CHANGED, "Turcat body is not a JSON array")
    return [row for row in data if isinstance(row, dict)]


@dataclass(frozen=True)
class TurcatIndicator:
    """One parsed Turcat row that is a real indicator (not a group header)."""

    code: str
    name: str
    unit: str | None
    frequency: str | None
    parent_code: str | None
    order: int
    period: date | None
    previous_period: date | None
    latest_value: Decimal | None
    previous_value: Decimal | None
    meta_url: str | None
    data_url: str | None
    page: str

    @property
    def points(self) -> list[tuple[date, Decimal | None]]:
        """The (latest, previous) observations with any period gaps removed."""
        points: list[tuple[date, Decimal | None]] = []
        if self.period is not None:
            points.append((self.period, self.latest_value))
        if self.previous_period is not None:
            points.append((self.previous_period, self.previous_value))
        return points


@dataclass(frozen=True)
class TurcatGroup:
    """A value-less group header that parents the indicators below it."""

    code: str
    name: str
    order: int
    parent_code: str | None
    meta_url: str | None


@dataclass(frozen=True)
class SectorParse:
    """Parsed result of one sector endpoint."""

    external_code: str
    page: str
    page_id: int
    indicators: list[TurcatIndicator] = field(default_factory=list)
    groups: list[TurcatGroup] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        """All INDICATOR codes, groups first in source order (groups included)."""
        entries = [(g.order, g.code) for g in self.groups] + [
            (i.order, i.code) for i in self.indicators
        ]
        return [code for _, code in sorted(entries)]


def _is_group(row: dict[str, Any]) -> bool:
    return all(
        row.get(field) in (None, "")
        for field in (
            "EN_SON_YAYIMLANAN_TARIHI",
            "EN_SON_YAYIMLANAN_VERI",
            "BIR_ONCEKI_DONEM_VERI",
            "BIRIMI",
        )
    )


def parse_sector(payload: bytes | str, external_code: str) -> SectorParse:
    """Parse one sector endpoint into indicators, groups and per-row errors."""
    if external_code not in SECTOR_BY_EXTERNAL_CODE:
        raise ConnectorError(FORMAT_CHANGED, f"unknown Turcat sector {external_code!r}")
    page_key, page, page_id = SECTOR_BY_EXTERNAL_CODE[external_code]
    rows = parse_turcat_payload(payload)

    indicators: list[TurcatIndicator] = []
    groups: list[TurcatGroup] = []
    errors: list[str] = []
    current_group: str | None = None
    # A header linked to an IMF SDDS category (``METAVERI``) opens a top-level section;
    # one without a link is a sub-heading of the section above it. Chaining every header
    # under the previous one buried "Merkezi Hükümet Operasyonları" four levels deep.
    current_section: str | None = None

    for order, row in enumerate(rows):
        row_id = row.get("ID")
        name = str(row.get("KATEGORI_BILESENLER") or "").strip()
        if row_id is None or not name:
            errors.append(f"row {order}: missing ID or KATEGORI_BILESENLER")
            continue
        code = str(row_id)
        meta_url = _clean_url(row.get("METAVERI"))
        data_url = _clean_url(row.get("EK_BILGI"))
        if _is_group(row):
            groups.append(
                TurcatGroup(
                    code=code,
                    name=name,
                    order=order,
                    parent_code=None if meta_url else current_section,
                    meta_url=meta_url,
                )
            )
            current_group = code
            if meta_url:
                current_section = code
            continue

        unit = _clean_text(row.get("BIRIMI"))
        period_raw = row.get("EN_SON_YAYIMLANAN_TARIHI")
        latest_raw = row.get("EN_SON_YAYIMLANAN_VERI")
        previous_raw = row.get("BIR_ONCEKI_DONEM_VERI")
        latest_value = parse_turcat_value(latest_raw)
        previous_value = parse_turcat_value(previous_raw)

        period: date | None = None
        frequency: str | None = None
        prev_period: date | None = None
        if period_raw not in (None, ""):
            try:
                period, frequency = parse_turcat_period(str(period_raw))
                prev_period = previous_period(frequency, period)
            except ConnectorError as exc:
                errors.append(f"row {code} ({name!r}): {exc.message}")
        elif latest_value is not None:
            errors.append(f"row {code} ({name!r}): value without a period")

        indicators.append(
            TurcatIndicator(
                code=code,
                name=name,
                unit=unit,
                frequency=frequency,
                parent_code=current_group,
                order=order,
                period=period,
                previous_period=prev_period,
                latest_value=latest_value,
                previous_value=previous_value,
                meta_url=meta_url,
                data_url=data_url,
                page=page,
            )
        )
    return SectorParse(
        external_code=external_code,
        page=page,
        page_id=page_id,
        indicators=indicators,
        groups=groups,
        errors=errors,
    )


def indicator_dimension_code() -> str:
    """The name of the dimension that holds one code per Turcat row."""
    return _INDICATOR_DIMENSION


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\xa0", " ").strip()
    return text or None


def _clean_url(value: Any) -> str | None:
    text = _clean_text(value)
    return text.rstrip() if text else None


__all__ = [
    "SECTORS",
    "SECTOR_BY_EXTERNAL_CODE",
    "SECTOR_BY_KEY",
    "SectorParse",
    "TurcatGroup",
    "TurcatIndicator",
    "indicator_dimension_code",
    "parse_sector",
    "parse_turcat_payload",
    "parse_turcat_period",
    "parse_turcat_value",
    "previous_period",
]
