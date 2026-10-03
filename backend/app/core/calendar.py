"""Release-calendar fetching, parsing and storage for the core refresh (1.4c).

Two live-verified sources:

- **EVDS3** (``calendar/aylikYayinlar?yil=&ay=``): a JSON list of items keyed by
  ``yayinBilgi.veriGrubuKodu`` (the datagroup code). Only ``bie_cli2`` is a core
  key; daily groups (``bie_dkefkytl``) are not in the calendar.
- **TÜİK** (``www.tuik.gov.tr/Kurumsal/GetYillikHaberBulteniListesi?yil=``): the
  published (``yayindaOlanlarList``) and upcoming (``yayindaOlayanlarList``)
  bulletins of every institution that shares the list. Only rows whose
  ``adi`` is the GDP bulletin and whose ``sorumluKisaAd`` is ``TÜİK`` count.

``sync_calendar`` keeps only rows whose key the core registry uses
(:data:`CALENDAR_KEYS`), upserts them by ``(source, key, expected_on)`` and never
swallows a source failure: the caller records the raised error.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    FORMAT_CHANGED,
    SOURCE_ERROR,
    THROTTLED,
    TIMEOUT,
    ConnectorError,
    ObjectStore,
    store_raw,
)
from app.connectors.tuik.parsers import is_throttle_response
from app.core.registry import CORE_SERIES
from app.data.models import ReleaseCalendar

logger = logging.getLogger(__name__)

EVDS3 = "evds3"
TUIK = "tuik"

#: The TÜİK bulletin ``adi`` that carries the quarterly GDP rows.
TUIK_GDP_KEY = "Dönemsel Gayrisafi Yurt İçi Hasıla"
TUIK_GDP_AGENCY = "TÜİK"

#: core external_code -> ``(source, key)``, or ``None`` for a calendar-less series.
CALENDAR_KEYS: dict[str, tuple[str, str] | None] = {}
for _core in CORE_SERIES:
    if _core.dataset_code == "bie_cli2":
        CALENDAR_KEYS[_core.external_code] = (EVDS3, "bie_cli2")
    elif _core.dataset_code == "bie_gsyhuretcar":
        CALENDAR_KEYS[_core.external_code] = (TUIK, TUIK_GDP_KEY)
    else:
        CALENDAR_KEYS[_core.external_code] = None
del _core


@dataclass(frozen=True)
class CalendarEntry:
    """One parsed calendar row, before it is filtered against ``CALENDAR_KEYS``."""

    source: str
    key: str
    period_label: str | None
    expected_on: date
    expected_at: str | None
    attributes: dict[str, Any]
    raw_object_key: str | None = None


@dataclass(frozen=True)
class CalendarSyncResult:
    """How many calendar rows a :func:`sync_calendar` run wrote."""

    inserted: int
    updated: int
    unchanged: int
    sources: tuple[str, ...]

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


# --- parsing ----------------------------------------------------------------


def _date_part(value: str | None) -> date | None:
    """The date part of a source timestamp (``2026-10-30 23:00`` or ISO)."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ").split(" ", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_evds_calendar(payload: Any) -> list[CalendarEntry]:
    """Parse an EVDS3 monthly calendar list into entries (core keys kept later)."""
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, "EVDS3 calendar is not a JSON array")
    entries: list[CalendarEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ConnectorError(FORMAT_CHANGED, "EVDS3 calendar item is not an object")
        info = item.get("yayinBilgi")
        if not isinstance(info, dict):
            continue
        key = info.get("veriGrubuKodu")
        if not key:
            # ``veriGrubuKodu`` is null for some rows: they are not calendar keys.
            continue
        expected_on = _date_part(item.get("tarih"))
        if expected_on is None:
            continue
        entries.append(
            CalendarEntry(
                source=EVDS3,
                key=str(key),
                period_label=item.get("donem"),
                expected_on=expected_on,
                expected_at=item.get("tarih"),
                attributes=item,
            )
        )
    return entries


def parse_tuik_calendar(payload: Any) -> list[CalendarEntry]:
    """Parse the TÜİK yearly bulletin list, keeping GDP rows published by TÜİK."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "TÜİK calendar is not a JSON object")
    entries: list[CalendarEntry] = []
    for list_name in ("yayindaOlanlarList", "yayindaOlmayanlarList"):
        rows = payload.get(list_name)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ConnectorError(FORMAT_CHANGED, f"TÜİK calendar {list_name} is not an array")
        for item in rows:
            if not isinstance(item, dict):
                raise ConnectorError(FORMAT_CHANGED, "TÜİK calendar item is not an object")
            if item.get("adi") != TUIK_GDP_KEY:
                continue
            if item.get("sorumluKisaAd") != TUIK_GDP_AGENCY:
                continue
            expected_on = _date_part(item.get("gTarih"))
            if expected_on is None:
                continue
            entries.append(
                CalendarEntry(
                    source=TUIK,
                    key=TUIK_GDP_KEY,
                    period_label=item.get("donemi"),
                    expected_on=expected_on,
                    expected_at=item.get("gTarih"),
                    attributes=item,
                )
            )
    return entries


def relevant_entries(entries: list[CalendarEntry]) -> list[CalendarEntry]:
    """Keep only rows whose ``(source, key)`` the core registry maps to."""
    wanted = {pair for pair in CALENDAR_KEYS.values() if pair is not None}
    return [entry for entry in entries if (entry.source, entry.key) in wanted]


# --- TÜİK HTTP client -------------------------------------------------------


@dataclass(frozen=True)
class TuikCalendarResponse:
    """A fetched TÜİK calendar body plus where it was stored."""

    content: bytes
    raw_object_key: str | None

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConnectorError(
                FORMAT_CHANGED,
                "TÜİK calendar body is not valid JSON",
                raw_object_key=self.raw_object_key,
            ) from exc


class TuikCalendarClient:
    """Retrying, throttle-aware client for the TÜİK yearly bulletin calendar."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = TUIK,
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = (base_url or resolved.tuik_veriportali_press_base_url).rstrip("/")
        self._path = "/Kurumsal/GetYillikHaberBulteniListesi"
        self._user_agent = resolved.tuik_veriportali_user_agent
        self._timeout_s = resolved.tuik_veriportali_request_timeout_s
        self._max_retries = resolved.tuik_veriportali_max_retries
        self._retry_backoff_s = resolved.tuik_veriportali_retry_backoff_s
        self._throttle_wait_s = resolved.tuik_veriportali_throttle_wait_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _store_raw(self, year: int, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(
                self._institution,
                "calendar",
                f"tuik-calendar-{year}",
                payload,
                ext,
                store=self._store,
            )
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("tuik calendar raw store failed for %d", year, exc_info=True)
            return None

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    @staticmethod
    def _error_message(content: bytes) -> str:
        return content[:200].decode("utf-8", errors="replace")

    def fetch(self, year: int) -> TuikCalendarResponse:
        """Fetch one year's bulletin list; raises ``ConnectorError`` on failure."""
        url = f"{self._base_url}{self._path}?yil={int(year)}"
        headers = {"User-Agent": self._user_agent, "Accept": "application/json, text/plain, */*"}
        attempt = 0
        while True:
            try:
                response = self._client.request("GET", url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT,
                        f"TÜİK calendar failed after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning("tuik calendar %s (attempt %d)", type(exc).__name__, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            content_type = response.headers.get("content-type")
            if response.status_code in (200, 206):
                if is_throttle_response(content_type, response.content):
                    key = self._store_raw(year, response.content, "html")
                    if attempt >= self._max_retries:
                        raise ConnectorError(
                            THROTTLED,
                            "TÜİK calendar returned a throttle (Yönlendiriliyor) page",
                            raw_object_key=key,
                        )
                    self._sleeper(self._throttle_wait_s * (2**attempt))
                    attempt += 1
                    continue
                key = self._store_raw(year, response.content, "json")
                return TuikCalendarResponse(response.content, key)
            if response.status_code >= 500:
                if attempt >= self._max_retries:
                    key = self._store_raw(year, response.content, "json")
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"TÜİK calendar HTTP {response.status_code}: "
                        f"{self._error_message(response.content)}",
                        raw_object_key=key,
                    )
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            key = self._store_raw(year, response.content, "json")
            raise ConnectorError(
                SOURCE_ERROR,
                f"TÜİK calendar HTTP {response.status_code}: "
                f"{self._error_message(response.content)}",
                raw_object_key=key,
            )


# --- sync -------------------------------------------------------------------


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def evds_months(now: datetime, *, back: int = 1, forward: int = 3) -> list[tuple[int, int]]:
    """Months to fetch from EVDS: previous month through ``+forward`` months."""
    year, month = now.year, now.month
    months: list[tuple[int, int]] = []
    for delta in range(-back, forward + 1):
        months.append(_add_months(year, month, delta))
    return months


def _upsert_entry(
    session: Session, entry: CalendarEntry, *, now: datetime
) -> tuple[int, int, int]:
    existing = session.scalar(
        sa.select(ReleaseCalendar).where(
            ReleaseCalendar.source == entry.source,
            ReleaseCalendar.key == entry.key,
            ReleaseCalendar.expected_on == entry.expected_on,
        )
    )
    if existing is None:
        session.add(
            ReleaseCalendar(
                source=entry.source,
                key=entry.key,
                period_label=entry.period_label,
                expected_on=entry.expected_on,
                expected_at=entry.expected_at,
                attributes=entry.attributes,
                fetched_at=now,
                raw_object_key=entry.raw_object_key,
            )
        )
        session.flush()
        return 1, 0, 0
    if (
        existing.period_label != entry.period_label
        or existing.expected_at != entry.expected_at
        or existing.attributes != entry.attributes
    ):
        existing.period_label = entry.period_label
        existing.expected_at = entry.expected_at
        existing.attributes = entry.attributes
        existing.fetched_at = now
        existing.raw_object_key = entry.raw_object_key
        session.flush()
        return 0, 1, 0
    session.flush()
    return 0, 0, 1


def sync_calendar(
    session: Session,
    *,
    now: datetime | None = None,
    evds_client: Any,
    tuik_fetch: Callable[[int], Any] | None,
) -> CalendarSyncResult:
    """Fetch both calendars and upsert the core-relevant rows.

    ``evds_client`` needs a ``get_calendar(year, month)`` method (or ``None`` to
    skip EVDS); ``tuik_fetch`` is either ``None`` (skip TÜİK) or a callable
    ``year -> response`` with ``.json()`` and ``raw_object_key``. A source
    failure is raised, never swallowed.
    """
    moment = now or datetime.now(UTC)
    entries: list[CalendarEntry] = []
    sources: list[str] = []

    if evds_client is not None:
        for year, month in evds_months(moment):
            response = evds_client.get_calendar(year, month)
            raw_key = getattr(response, "raw_object_key", None)
            for entry in relevant_entries(parse_evds_calendar(response.json())):
                entries.append(replace(entry, raw_object_key=raw_key))
        sources.append(EVDS3)

    if tuik_fetch is not None:
        for year in (moment.year, moment.year + 1):
            response = tuik_fetch(year)
            raw_key = getattr(response, "raw_object_key", None)
            for entry in relevant_entries(parse_tuik_calendar(response.json())):
                entries.append(replace(entry, raw_object_key=raw_key))
        sources.append(TUIK)

    # De-duplicate identical (source, key, expected_on) rows within one sync
    # (the EVDS window can touch the same date through adjacent month calls).
    deduped: dict[tuple[str, str, date], CalendarEntry] = {}
    for entry in entries:
        deduped[(entry.source, entry.key, entry.expected_on)] = entry

    inserted = updated = unchanged = 0
    for entry in deduped.values():
        ins, upd, unch = _upsert_entry(session, entry, now=moment)
        inserted += ins
        updated += upd
        unchanged += unch
    return CalendarSyncResult(inserted, updated, unchanged, tuple(sources))


__all__ = [
    "CALENDAR_KEYS",
    "EVDS3",
    "TUIK",
    "TUIK_GDP_AGENCY",
    "TUIK_GDP_KEY",
    "CalendarEntry",
    "CalendarSyncResult",
    "TuikCalendarClient",
    "TuikCalendarResponse",
    "evds_months",
    "parse_evds_calendar",
    "parse_tuik_calendar",
    "relevant_entries",
    "sync_calendar",
]
