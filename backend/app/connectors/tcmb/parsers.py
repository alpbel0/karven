"""Pure parsers for the TCMB EVDS3 JSON channel.

EVDS3 is key-less plain JSON: a category/group catalog, a per-group series list,
per-series bounds and one data message per series. Nothing here touches the
network or the database, so every shape is fixture-testable.

Period strings are exact and must never be guessed. ``Tarih`` depends on the
frequency code:

- ``1`` daily / ``2`` workday / ``3`` weekly / ``4`` twice a month -> ``dd-mm-yyyy``
- ``5`` monthly -> ``yyyy-mm`` (period start = day 1)
- ``6`` quarterly -> ``yyyy-nÇ`` (period start = first day of the quarter)
- ``8`` yearly -> ``yyyy`` (period start = Jan 1)

An unknown shape, frequency or aggregation raises :class:`ConnectorError` with
kind ``format_changed``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.connectors.base import FORMAT_CHANGED, ConnectorError
from app.data.periods import (
    ANNUAL,
    DAILY,
    IRREGULAR,
    MONTHLY,
    QUARTERLY,
    WEEKLY,
)

# Aggregation methods EVDS3 exposes; the ``*ABLE`` flags select the supported subset.
AGGREGATIONS: tuple[str, ...] = ("avg", "last", "sum", "min", "max")

# Source frequency code -> our frequency. Codes seen live; anything else is a
# format change, never a guess.
FREQUENCY_BY_CODE: dict[str, str] = {
    "1": DAILY,
    "2": DAILY,
    "3": WEEKLY,
    "4": IRREGULAR,
    "5": MONTHLY,
    "6": QUARTERLY,
    "8": ANNUAL,
}

# Exact source labels, normalized to ASCII upper, map to the numeric code. Used
# both to derive our frequency and to cross-check the bounds' frequency.
FREQUENCY_CODE_BY_LABEL: dict[str, str] = {
    "GUNLUK": "1",
    "IS GUNU": "2",
    "HAFTALIK(CUMA)": "3",
    "AYDA IKI KEZ": "4",
    "AYLIK": "5",
    "UC AYLIK": "6",
    "YILLIK": "8",
}

FREQUENCY_BY_LABEL: dict[str, str] = {
    label: FREQUENCY_BY_CODE[code] for label, code in FREQUENCY_CODE_BY_LABEL.items()
}

# ABLE flag -> the aggregation name it enables.
_AGGREGATION_FLAGS: tuple[tuple[str, str], ...] = (
    ("AVGABLE", "avg"),
    ("FIRSTABLE", "first"),
    ("LASTABLE", "last"),
    ("MAXABLE", "max"),
    ("MINABLE", "min"),
    ("SUMABLE", "sum"),
)

# Turkish letters are mapped explicitly: ``.lower()`` mangles İ/I, and the source
# already upper-cases its labels.
_TR_TRANSLATE = str.maketrans(
    {"İ": "I", "I": "I", "Ş": "S", "Ğ": "G", "Ü": "U", "Ö": "O", "Ç": "C"}
)

_MONTHLY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTERLY_RE = re.compile(r"^(\d{4})-([1-4])Ç$")
_YEARLY_RE = re.compile(r"^(\d{4})$")
_PLAIN_DECIMAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse(value: Any) -> str | None:
    """Collapse runs of whitespace (SERIE_NAME often has double spaces)."""
    if value is None:
        return None
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    return text or None


def normalize_frequency_label(text: Any) -> str:
    """Upper-case a source frequency string and ASCII-fold its Turkish letters."""
    return str(text).strip().upper().translate(_TR_TRANSLATE)


def frequency_for_label(text: Any) -> str:
    """Map a source ``FREQUENCY_STR`` to one of our frequencies."""
    normalized = normalize_frequency_label(text)
    code = FREQUENCY_CODE_BY_LABEL.get(normalized)
    if code is None:
        raise ConnectorError(FORMAT_CHANGED, f"unknown TCMB source frequency {text!r}")
    return FREQUENCY_BY_CODE[code]


def frequency_code_for_label(text: Any) -> str:
    """The source's numeric frequency code for a ``FREQUENCY_STR`` label."""
    normalized = normalize_frequency_label(text)
    code = FREQUENCY_CODE_BY_LABEL.get(normalized)
    if code is None:
        raise ConnectorError(FORMAT_CHANGED, f"unknown TCMB source frequency {text!r}")
    return code


def _require_keys(obj: dict[str, Any], keys: tuple[str, ...], *, context: str) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise ConnectorError(FORMAT_CHANGED, f"TCMB {context} is missing keys {missing}")


def _flag(value: Any) -> bool:
    return value in (1, True) or str(value).strip() in ("1", "True", "true")


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ConnectorError(FORMAT_CHANGED, f"TCMB expected an integer, got {value!r}") from exc


@dataclass(frozen=True)
class CategoryPathEntry:
    """One ancestor of a datagroup in the EVDS3 category tree (root first)."""

    id: int
    level: int
    title: str
    title_en: str | None


# ``CATEGORY_ID -> (parent id, level, title_tr, title_en)`` for the whole tree.
_CategoryNode = tuple[int | None, int, str, str | None]


def _category_path(
    category_id: int | None, index: dict[int, _CategoryNode]
) -> tuple[CategoryPathEntry, ...]:
    """Walk ``UST_CATEGORY_ID`` up to the root; unknowns end the walk, cycles fail."""
    entries: list[CategoryPathEntry] = []
    seen: set[int] = set()
    current = category_id
    while current is not None and current != -1:
        if current in seen:
            raise ConnectorError(FORMAT_CHANGED, f"TCMB category cycle at {current!r}")
        seen.add(current)
        node = index.get(current)
        if node is None:
            break
        parent, level, title, title_en = node
        entries.append(CategoryPathEntry(id=current, level=level, title=title, title_en=title_en))
        current = parent
    entries.reverse()
    return tuple(entries)


@dataclass(frozen=True)
class CatalogGroup:
    """One source-owned EVDS3 datagroup from the catalog."""

    code: str
    name: str
    name_en: str | None
    category_id: str | None
    category_title: str
    category_title_en: str | None
    source_frequency: str | None
    data_source_en: str
    unit: str | None
    unit_en: str | None
    source_last_updated: str | None
    screen_order: int | None
    category_path: tuple[CategoryPathEntry, ...] = ()


def parse_catalog(payload: Any, data_source: str = "CBRT") -> list[CatalogGroup]:
    """Return only the groups owned by ``data_source``, in a deterministic order.

    A group is ours only when its ``DATASOURCE_ENG`` is exactly ``data_source``
    (``CBRT`` by default); mixed groups (``"CBRT, Settlement and Custody Bank"``)
    and other agencies are excluded. The full category index is built first so
    every kept group carries its root-first ``category_path``.
    """
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, "TCMB catalog is not a JSON array")
    index: dict[int, _CategoryNode] = {}
    for category in payload:
        if not isinstance(category, dict):
            raise ConnectorError(FORMAT_CHANGED, "TCMB catalog category is not an object")
        _require_keys(
            category,
            ("TOPIC_TITLE_TR", "CATEGORY_ID", "SEVIYE", "UST_CATEGORY_ID"),
            context="catalog category",
        )
        category_id = _as_int(category["CATEGORY_ID"])
        if category_id is None:
            raise ConnectorError(FORMAT_CHANGED, "TCMB catalog category has no CATEGORY_ID")
        level = _as_int(category["SEVIYE"])
        if level is None:
            raise ConnectorError(FORMAT_CHANGED, "TCMB catalog category has no SEVIYE")
        index[category_id] = (
            _as_int(category["UST_CATEGORY_ID"]),
            level,
            _collapse(category["TOPIC_TITLE_TR"]) or "",
            _collapse(category.get("TOPIC_TITLE_ENG")),
        )

    groups: list[tuple[int, int, str, CatalogGroup]] = []
    for category_order, category in enumerate(payload):
        category_title = _collapse(category.get("TOPIC_TITLE_TR")) or ""
        category_title_en = _collapse(category.get("TOPIC_TITLE_ENG"))
        category_id = category.get("CATEGORY_ID")
        category_path = _category_path(_as_int(category_id), index)
        raw_groups = category.get("DATAGROUPS") or []
        if not isinstance(raw_groups, list):
            raise ConnectorError(FORMAT_CHANGED, "TCMB DATAGROUPS is not a JSON array")
        for group_order, group in enumerate(raw_groups):
            if not isinstance(group, dict):
                raise ConnectorError(FORMAT_CHANGED, "TCMB catalog group is not an object")
            _require_keys(
                group,
                ("DATAGROUP_CODE", "DATAGROUP_TYPE", "DATASOURCE_ENG"),
                context="catalog group",
            )
            if group["DATASOURCE_ENG"] != data_source:
                continue
            code = str(group["DATAGROUP_CODE"]).strip()
            if not code:
                raise ConnectorError(FORMAT_CHANGED, "TCMB catalog group has an empty code")
            screen_order = _as_int(group.get("screenOrder"))
            meta = CatalogGroup(
                code=code,
                name=_collapse(group["DATAGROUP_TYPE"]) or code,
                name_en=_collapse(group.get("DATAGROUP_TYPE_ENG")),
                category_id=str(category_id).strip() if category_id is not None else None,
                category_title=category_title,
                category_title_en=category_title_en,
                source_frequency=_collapse(group.get("FREQUENCY_STR")),
                data_source_en=str(group["DATASOURCE_ENG"]).strip(),
                unit=_collapse(group.get("BIRIMI")),
                unit_en=_collapse(group.get("BIRIMI_EN")),
                source_last_updated=_collapse(group.get("LAST_UPDATED")),
                screen_order=screen_order,
                category_path=category_path,
            )
            # category order, then screenOrder, then code (None sorts first).
            groups.append((category_order, group_order, screen_order or 0, meta))
    groups.sort(key=lambda item: (item[0], item[2], item[3].code))
    return [meta for _, _, _, meta in groups]


@dataclass(frozen=True)
class SerieInfo:
    """One series of an EVDS3 datagroup."""

    code: str
    group_code: str
    name: str
    name_en: str | None
    frequency: str
    source_frequency: str
    aggregation: str
    aggregations: list[str] = field(default_factory=list)
    level: int | None = None
    parent_code: str | None = None
    screen_order: int | None = None


def parse_serie_list(payload: Any, group_code: str) -> list[SerieInfo]:
    """Parse one group's series list, collapsing whitespace and mapping frequencies."""
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, f"TCMB serie list for {group_code!r} is not an array")
    series: list[SerieInfo] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ConnectorError(FORMAT_CHANGED, "TCMB serie list row is not an object")
        _require_keys(
            row,
            (
                "SERIE_CODE",
                "SERIE_NAME",
                "FREQUENCY_STR",
                "DEFAULT_AGG_METHOD",
                "UST_SERIE_CODE",
                "SEVIYE",
            ),
            context=f"serie list row of {group_code!r}",
        )
        code = str(row["SERIE_CODE"]).strip()
        if not code:
            raise ConnectorError(FORMAT_CHANGED, "TCMB serie list row has an empty code")
        source_frequency = _collapse(row["FREQUENCY_STR"]) or ""
        frequency = frequency_for_label(source_frequency)
        aggregation = str(row["DEFAULT_AGG_METHOD"]).strip()
        if aggregation not in AGGREGATIONS:
            raise ConnectorError(
                FORMAT_CHANGED, f"unknown TCMB aggregation {aggregation!r} for {code!r}"
            )
        parent_raw = str(row["UST_SERIE_CODE"]).strip()
        parent_code = None if parent_raw in ("", "-1") else parent_raw
        aggregations = [name for flag, name in _AGGREGATION_FLAGS if _flag(row.get(flag))]
        series.append(
            SerieInfo(
                code=code,
                group_code=str(row.get("DATAGROUP_CODE") or group_code).strip(),
                name=_collapse(row["SERIE_NAME"]) or code,
                name_en=_collapse(row.get("SERIE_NAME_ENG")),
                frequency=frequency,
                source_frequency=source_frequency,
                aggregation=aggregation,
                aggregations=aggregations,
                level=_as_int(row["SEVIYE"]),
                parent_code=parent_code,
                screen_order=_as_int(row.get("SCREEN_ORDER")),
            )
        )
    series.sort(key=lambda item: (item.screen_order or 0, item.code))
    return series


def _parse_evds_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConnectorError(FORMAT_CHANGED, f"TCMB date must be a string, got {value!r}")
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y").date()
    except ValueError as exc:
        raise ConnectorError(FORMAT_CHANGED, f"invalid TCMB date {value!r}") from exc


@dataclass(frozen=True)
class Bounds:
    """One series' UI window plus its real coverage and frequency code."""

    start_date: date | None
    end_date: date | None
    max_start_date: date | None
    min_end_date: date | None
    frequency_code: str


def parse_bounds(payload: Any) -> Bounds:
    """Parse the bounds response; dates may be ``None`` (series with no data)."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "TCMB bounds is not a JSON object")
    _require_keys(
        payload,
        ("startDate", "endDate", "maxStartDate", "minEndDate", "frequency"),
        context="bounds",
    )
    frequency_code = str(payload["frequency"]).strip()
    if frequency_code not in FREQUENCY_BY_CODE:
        raise ConnectorError(FORMAT_CHANGED, f"unknown TCMB frequency code {frequency_code!r}")
    return Bounds(
        start_date=_parse_evds_date(payload["startDate"]),
        end_date=_parse_evds_date(payload["endDate"]),
        max_start_date=_parse_evds_date(payload["maxStartDate"]),
        min_end_date=_parse_evds_date(payload["minEndDate"]),
        frequency_code=frequency_code,
    )


def parse_period(text: Any, frequency_code: str) -> date:
    """Turn a ``Tarih`` value into the period start date for ``frequency_code``.

    Daily/weekly/irregular and monthly/quarterly ``Tarih`` values are strings.
    The yearly frequency (code ``8``) arrives as a JSON integer from the live
    EVDS3 API; an integer in ``1000..9999`` is accepted there alongside the
    string form, and rejected (never guessed) for every other frequency.
    """
    if frequency_code not in FREQUENCY_BY_CODE:
        raise ConnectorError(FORMAT_CHANGED, f"unknown TCMB frequency code {frequency_code!r}")
    if frequency_code == "8" and isinstance(text, int) and not isinstance(text, bool):
        if not 1000 <= text <= 9999:
            raise ConnectorError(FORMAT_CHANGED, f"unrecognized yearly TCMB Tarih {text!r}")
        return date(text, 1, 1)
    if not isinstance(text, str):
        raise ConnectorError(FORMAT_CHANGED, f"TCMB Tarih must be a string, got {text!r}")
    value = text.strip()
    if frequency_code in ("1", "2", "3", "4"):
        parsed = _parse_evds_date(value)
        assert parsed is not None  # value is a non-empty string here
        return parsed
    if frequency_code == "5":
        match = _MONTHLY_RE.fullmatch(value)
        if match is None:
            raise ConnectorError(FORMAT_CHANGED, f"unrecognized monthly TCMB Tarih {text!r}")
        month = int(match[2])
        if not 1 <= month <= 12:
            raise ConnectorError(FORMAT_CHANGED, f"unrecognized monthly TCMB Tarih {text!r}")
        return date(int(match[1]), month, 1)
    if frequency_code == "6":
        match = _QUARTERLY_RE.fullmatch(value)
        if match is None:
            raise ConnectorError(FORMAT_CHANGED, f"unrecognized quarterly TCMB Tarih {text!r}")
        return date(int(match[1]), (int(match[2]) - 1) * 3 + 1, 1)
    match = _YEARLY_RE.fullmatch(value)
    if match is None:
        raise ConnectorError(FORMAT_CHANGED, f"unrecognized yearly TCMB Tarih {text!r}")
    return date(int(match[1]), 1, 1)


def _parse_plain_decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ConnectorError(FORMAT_CHANGED, f"TCMB value must be a string, got {value!r}")
    text = value.strip()
    if not _PLAIN_DECIMAL_RE.fullmatch(text):
        raise ConnectorError(FORMAT_CHANGED, f"invalid TCMB value {value!r}")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise ConnectorError(FORMAT_CHANGED, f"invalid TCMB value {value!r}") from exc


def parse_data(payload: Any, serie_code: str, frequency_code: str) -> list[tuple[date, Decimal]]:
    """Parse one FE data message into ascending ``(period, value)`` points.

    Null rows are skipped (never written as 0), the value column is derived from
    the series code, and duplicate periods are a format change.
    """
    if frequency_code not in FREQUENCY_BY_CODE:
        raise ConnectorError(FORMAT_CHANGED, f"unknown TCMB frequency code {frequency_code!r}")
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "TCMB data message is not a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ConnectorError(FORMAT_CHANGED, "TCMB data message has no items array")
    column = serie_code.replace(".", "_")
    allowed = {"Tarih", "UNIXTIME", column}
    points: dict[date, Decimal] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ConnectorError(FORMAT_CHANGED, "TCMB data item is not an object")
        unknown = set(item) - allowed
        if unknown:
            raise ConnectorError(FORMAT_CHANGED, f"unexpected TCMB data keys {sorted(unknown)}")
        if "Tarih" not in item or column not in item:
            raise ConnectorError(
                FORMAT_CHANGED, f"TCMB data item is missing {sorted(allowed - set(item))}"
            )
        raw_value = item[column]
        if raw_value is None:
            continue
        value = _parse_plain_decimal(raw_value)
        period = parse_period(item["Tarih"], frequency_code)
        if period in points:
            raise ConnectorError(FORMAT_CHANGED, f"duplicate TCMB period {period.isoformat()}")
        points[period] = value
    return sorted(points.items())


__all__ = [
    "AGGREGATIONS",
    "FREQUENCY_BY_CODE",
    "FREQUENCY_BY_LABEL",
    "FREQUENCY_CODE_BY_LABEL",
    "Bounds",
    "CatalogGroup",
    "CategoryPathEntry",
    "SerieInfo",
    "frequency_code_for_label",
    "frequency_for_label",
    "normalize_frequency_label",
    "parse_bounds",
    "parse_catalog",
    "parse_data",
    "parse_period",
    "parse_serie_list",
]
