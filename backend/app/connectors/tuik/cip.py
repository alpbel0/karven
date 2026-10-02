"""TÜİK CİP (Coğrafi İstatistik Portalı) connector — regional indicators.

The portal exposes one token-free JSON endpoint that reduces MEDAS/ilGostergeleri
regional indicators to a single GET request::

    GET https://cip.tuik.gov.tr/Home/GetMapData
        ?kaynak=<medas|ilGostergeleri|json>&duzey=<1..4>
        &gostergeNo=<code>&kayitSayisi=<N>&period=<yillik|aylik|...>

Both success and failure answer ``HTTP 200 + application/json``; an error body is
``{"Message": ..., "StatucCode": 500}`` with no ``veriler`` array, so success is
detected from the body, never from the status code.

Catalog source is the live ``assets/sideMenu.json`` menu (80 indicators across
``medas`` and ``ilGostergeleri``). The full MEDAS indicator list from earlier
offline research is *not* exposed by any live endpoint, so the catalog is built
from sideMenu and every candidate level is verified live. Each indicator becomes
one dataset (``CIP_<gostergeNo>``) with ``LEVEL`` (1-4), ``REF_AREA`` (the geo
codes the source returns, with parents derived from the published İBBS/NUTS
geometries) and ``TIME_PERIOD`` dimensions. Missing/blank cells are stored as
``NULL`` — the source cannot distinguish a true zero from suppression, which is
recorded on the dataset.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
    SOURCE_ERROR,
    TIMEOUT,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    MinioObjectStore,
    ObjectStore,
    SourceConnector,
    store_raw,
)
from app.connectors.tuik.client import RawResponse
from app.connectors.tuik.hierarchy import derive_nuts_parent
from app.connectors.tuik.parsers import parse_value
from app.data.periods import frequency_for_sdmx_code

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cip.tuik.gov.tr"
SIDE_MENU_PATH = "/assets/sideMenu.json"
MAP_DATA_PATH = "/Home/GetMapData"
GEOMETRY_PATH = "/assets/geometri/nuts{level}.json"

CHANNEL = "cip"
SIDE_MENU_CHANNEL = "cip-catalog"
GEOMETRY_CHANNEL = "cip-geometri"

LEVEL_DIMENSION = "LEVEL"
REF_AREA_DIMENSION = "REF_AREA"
TIME_DIMENSION = "TIME_PERIOD"

SUPPORTED_LEVELS = (1, 2, 3, 4)
LEVEL_LABELS = {
    1: "İBBS-1 bölgesi",
    2: "İBBS-2 bölgesi",
    3: "İl (İBBS-3)",
    4: "İlçe",
}
# CİP does not publish nuts1.json (HTTP 404); the 12 İBBS-1 region names are fixed.
IBBS1_LABELS = {
    "TR1": "İstanbul",
    "TR2": "Batı Marmara",
    "TR3": "Ege",
    "TR4": "Doğu Marmara",
    "TR5": "Batı Anadolu",
    "TR6": "Akdeniz",
    "TR7": "Orta Anadolu",
    "TR8": "Batı Karadeniz",
    "TR9": "Doğu Karadeniz",
    "TRA": "Kuzeydoğu Anadolu",
    "TRB": "Ortadoğu Anadolu",
    "TRC": "Güneydoğu Anadolu",
}

# ``period`` values seen in sideMenu mapped to SDMX frequency codes.
PERIOD_FREQ_CODES = {
    "yillik": "A",
    "aylik": "M",
    "ceyrek": "Q",
    "ceyreklik": "Q",
    "haftalik": "W",
    "gunluk": "D",
}

# ``kayitSayisi`` is the number of periods to return. Catalog probes want the full
# history (research observed at most 30 periods); fetches ask for the same.
CATALOG_KAYIT_SAYISI = 200
FETCH_KAYIT_SAYISI = 500

NULL_NOTE = (
    "CİP cannot distinguish a true zero from suppression: blank cells are stored "
    "as NULL, never 0. Explicit '0' values from the source are kept."
)

_MISSING_STATUS = "Success"


@dataclass(frozen=True)
class CipEntry:
    """One indicator from ``sideMenu.json``."""

    gosterge_no: str
    name: str
    name_en: str | None
    kaynak: str
    period: str
    menu_name: str | None
    levels: tuple[int, ...]
    kayit_sayisi: int | None = None


@dataclass(frozen=True)
class CipUnit:
    """One geographic unit row of a GetMapData response."""

    code: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class CipMapData:
    """Parsed GetMapData payload."""

    gosterge_no: str
    name: str
    name_en: str | None
    period: str
    decimal_precision: str | None
    metadata_url: str | None
    periods: tuple[str, ...]
    units: tuple[CipUnit, ...]

    @property
    def obs_count(self) -> int:
        return len(self.units) * len(self.periods)


@dataclass(frozen=True)
class GeometryUnit:
    """One geometry feature's properties."""

    code: str
    label: str
    region_code: str | None = None
    nuts_code: str | None = None


@dataclass(frozen=True)
class CipCatalogFailure:
    """A sideMenu entry that returned no data at any of its levels."""

    gosterge_no: str
    kind: str
    reason: str


@dataclass(frozen=True)
class CipLevelProbe:
    """A working level of one indicator (units + periods)."""

    level: int
    data: CipMapData


def parse_side_menu(payload: Any) -> list[CipEntry]:
    """Parse ``sideMenu.json`` into unique :class:`CipEntry` records."""
    if not isinstance(payload, dict) or payload.get("status") != _MISSING_STATUS:
        raise ConnectorError(FORMAT_CHANGED, "sideMenu.json is not a Success object")
    menu = payload.get("menu")
    if not isinstance(menu, list):
        raise ConnectorError(FORMAT_CHANGED, "sideMenu.json has no menu[] list")
    entries: dict[str, CipEntry] = {}
    for group in menu:
        if not isinstance(group, dict):
            continue
        menu_name = group.get("menuName")
        for sub in group.get("subMenu") or []:
            if not isinstance(sub, dict):
                continue
            gosterge_no = sub.get("gostergeNo")
            if not gosterge_no:
                continue
            gosterge_no = str(gosterge_no)
            levels = tuple(
                level
                for level in (sub.get("duzeyler") or [])
                if isinstance(level, int) and level in SUPPORTED_LEVELS
            )
            entry = CipEntry(
                gosterge_no=gosterge_no,
                name=str(sub.get("gostergeAdi") or gosterge_no),
                name_en=sub.get("gostergeAdiEn"),
                kaynak=str(sub.get("kaynak") or "medas"),
                period=str(sub.get("period") or "yillik"),
                menu_name=menu_name,
                levels=levels,
                kayit_sayisi=sub.get("kayitSayisi"),
            )
            existing = entries.get(gosterge_no)
            if existing is None or len(entry.levels) > len(existing.levels):
                entries[gosterge_no] = entry
    return list(entries.values())


def is_error_payload(payload: Any) -> bool:
    """True when the body is the generic 200-with-error response."""
    return (
        isinstance(payload, dict)
        and payload.get("Message") is not None
        and not payload.get("veriler")
    )


def parse_map_data(payload: Any, *, raw_object_key: str | None = None) -> CipMapData:
    """Parse a GetMapData payload; raise a typed error for the error body."""
    if not isinstance(payload, dict):
        raise ConnectorError(
            FORMAT_CHANGED,
            "CİP GetMapData response is not a JSON object",
            raw_object_key=raw_object_key,
        )
    if is_error_payload(payload):
        message = str(payload.get("Message") or "unknown CİP error")
        status = payload.get("StatucCode") or payload.get("StatusCode")
        raise ConnectorError(
            NOT_FOUND,
            f"CİP error body: {message} (StatucCode={status})",
            raw_object_key=raw_object_key,
        )
    periods = payload.get("tarihler")
    units_raw = payload.get("veriler")
    if not isinstance(units_raw, list) or not units_raw:
        raise ConnectorError(
            EMPTY,
            "CİP response has no veriler[] units",
            raw_object_key=raw_object_key,
        )
    if not isinstance(periods, list):
        raise ConnectorError(
            FORMAT_CHANGED,
            "CİP response is missing tarihler[]",
            raw_object_key=raw_object_key,
        )
    units: list[CipUnit] = []
    for row in units_raw:
        if not isinstance(row, dict) or row.get("duzeyKodu") is None:
            continue
        # ``veri`` is the live key; tolerate the ``veriler`` spelling seen in notes.
        values = row.get("veri")
        if values is None:
            values = row.get("veriler")
        if not isinstance(values, list):
            values = []
        units.append(CipUnit(code=str(row["duzeyKodu"]), values=tuple(values)))
    return CipMapData(
        gosterge_no=str(payload.get("gostergeNo") or ""),
        name=str(payload.get("gosterge_ad") or ""),
        name_en=payload.get("gosterge_ad_ing"),
        period=str(payload.get("period") or "yillik"),
        decimal_precision=payload.get("ondalikHassasiyet"),
        metadata_url=payload.get("metaVeriURL"),
        periods=tuple(str(period) for period in periods),
        units=tuple(units),
    )


def parse_geometry(payload: Any) -> dict[str, GeometryUnit]:
    """Parse a ``nuts{level}.json`` FeatureCollection into ``code -> unit``."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "CİP geometry is not an object")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ConnectorError(FORMAT_CHANGED, "CİP geometry has no features[]")
    units: dict[str, GeometryUnit] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict) or props.get("duzeyKodu") is None:
            continue
        code = str(props["duzeyKodu"])
        units[code] = GeometryUnit(
            code=code,
            label=str(props.get("ad") or props.get("name") or code),
            region_code=(
                str(props["bolgeKodu"]) if props.get("bolgeKodu") not in (None, "") else None
            ),
            nuts_code=str(props["nutsKodu"]) if props.get("nutsKodu") else None,
        )
    return units


def period_start(text: str, period: str) -> date:
    """Parse a CİP period label (``2025`` or ``2026/8``) into a start date."""
    value = str(text).strip()
    if "/" in value:
        year_part, _, month_part = value.partition("/")
        try:
            year = int(year_part)
            month = int(month_part)
        except ValueError as exc:
            raise ConnectorError(FORMAT_CHANGED, f"unrecognized CİP period {text!r}") from exc
        if not 1 <= month <= 12:
            raise ConnectorError(FORMAT_CHANGED, f"invalid month in CİP period {text!r}")
        return date(year, month, 1)
    try:
        return date(int(value), 1, 1)
    except ValueError as exc:
        raise ConnectorError(FORMAT_CHANGED, f"unrecognized CİP period {text!r}") from exc


def frequency_code(period: str) -> str:
    """Map a CİP ``period`` to its SDMX frequency code (defaults to annual)."""
    return PERIOD_FREQ_CODES.get(str(period).strip().lower(), "A")


def unit_hint(name: str) -> str | None:
    """Extract a trailing parenthetical unit from an indicator name, if any."""
    end = name.rfind("(")
    if end == -1 or not name.rstrip().endswith(")"):
        return None
    hint = name[end + 1 :].rstrip()
    hint = hint[: hint.rfind(")")].strip()
    return hint or None


class CipClient:
    """Retrying, raw-storing HTTP client for the CİP portal."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = "tuik",
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = base_url.rstrip("/")
        self._timeout_s = resolved.cip_request_timeout_s
        self._max_retries = resolved.cip_max_retries
        self._retry_backoff_s = resolved.cip_retry_backoff_s
        self._max_concurrency = resolved.cip_max_concurrency
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _store_raw(self, dataset: str, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(self._institution, dataset, channel, payload, ext, store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("cip raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    @staticmethod
    def _error_message(content: bytes) -> str:
        try:
            payload = content.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return "<undecodable body>"
        return payload[:300]

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    def _request(self, path: str, *, params: Mapping[str, Any], dataset: str, channel: str):
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"CİP request timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("cip timeout on %s (attempt %d)", path, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"CİP transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning("cip transport error on %s (attempt %d)", path, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            if response.status_code in (200, 206):
                return response
            if response.status_code in (404, 410):
                key = self._store_raw(dataset, f"{channel}-error", response.content, "json")
                raise ConnectorError(
                    NOT_FOUND,
                    f"HTTP {response.status_code}: {self._error_message(response.content)}",
                    raw_object_key=key,
                )
            if response.status_code >= 500:
                if attempt >= self._max_retries:
                    key = self._store_raw(dataset, f"{channel}-error", response.content, "json")
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"HTTP {response.status_code}: {self._error_message(response.content)}",
                        raw_object_key=key,
                    )
                logger.warning(
                    "cip HTTP %d on %s (attempt %d)", response.status_code, path, attempt + 1
                )
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            key = self._store_raw(dataset, f"{channel}-error", response.content, "json")
            raise ConnectorError(
                SOURCE_ERROR,
                f"HTTP {response.status_code}: {self._error_message(response.content)}",
                raw_object_key=key,
            )

    def _raw(self, response: httpx.Response, dataset: str, channel: str) -> RawResponse:
        key = self._store_raw(dataset, channel, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def side_menu(self) -> RawResponse:
        """Fetch the live side menu catalog."""
        response = self._request(
            SIDE_MENU_PATH, params={}, dataset="sideMenu", channel=SIDE_MENU_CHANNEL
        )
        return self._raw(response, "sideMenu", SIDE_MENU_CHANNEL)

    def map_data(
        self,
        *,
        kaynak: str,
        duzey: int,
        gosterge_no: str,
        kayit_sayisi: int,
        period: str,
    ) -> RawResponse:
        """Fetch one indicator at one geographic level."""
        params = {
            "kaynak": kaynak,
            "duzey": duzey,
            "gostergeNo": gosterge_no,
            "kayitSayisi": kayit_sayisi,
            "period": period,
        }
        response = self._request(MAP_DATA_PATH, params=params, dataset=gosterge_no, channel=CHANNEL)
        return self._raw(response, gosterge_no, CHANNEL)

    def geometry(self, level: int) -> RawResponse:
        """Fetch the İBBS/NUTS geometry properties for one level."""
        path = GEOMETRY_PATH.format(level=level)
        response = self._request(path, params={}, dataset=f"nuts{level}", channel=GEOMETRY_CHANNEL)
        return self._raw(response, f"nuts{level}", GEOMETRY_CHANNEL)


class CipConnector(SourceConnector):
    """Lists and fetches TÜİK CİP regional indicators."""

    institution_code = "tuik"
    institution_name = "Türkiye İstatistik Kurumu"
    channel = CHANNEL

    def __init__(
        self,
        *,
        client: CipClient | None = None,
        store: ObjectStore | None = None,
        http_client: Any = None,
        base_url: str = DEFAULT_BASE_URL,
        settings_obj: Settings | None = None,
    ) -> None:
        if client is None:
            if store is None:
                store = MinioObjectStore(settings_obj)
            client = CipClient(
                base_url=base_url,
                http_client=http_client,
                store=store,
                institution=self.institution_code,
                settings_obj=settings_obj,
            )
        self._client = client
        self._entries: list[CipEntry] | None = None
        self._side_menu_raw_key: str | None = None
        self._geometry: dict[int, dict[str, GeometryUnit]] = {}
        self._geometry_lock = threading.Lock()
        self._failures: dict[str, CipCatalogFailure] = {}
        self._failures_lock = threading.Lock()

    @property
    def client(self) -> CipClient:
        return self._client

    @property
    def side_menu_raw_object_key(self) -> str | None:
        return self._side_menu_raw_key

    def close(self) -> None:
        self._client.close()

    def failures(self) -> list[CipCatalogFailure]:
        """Every sideMenu entry that produced no data at any level so far."""
        with self._failures_lock:
            return list(self._failures.values())

    def _record_failure(self, entry: CipEntry, error: ConnectorError) -> None:
        with self._failures_lock:
            self._failures[entry.gosterge_no] = CipCatalogFailure(
                gosterge_no=entry.gosterge_no, kind=error.kind, reason=error.message
            )

    def entries(self) -> list[CipEntry]:
        """The live indicator catalog (parsed from sideMenu.json)."""
        if self._entries is None:
            response = self._client.side_menu()
            self._side_menu_raw_key = response.raw_object_key
            self._entries = parse_side_menu(response.json())
            logger.info("cip catalog: %d indicators", len(self._entries))
        return self._entries

    def _resolve_entry(self, dataset_code: str | CipEntry) -> CipEntry:
        if isinstance(dataset_code, CipEntry):
            return dataset_code
        gosterge_no = dataset_code[4:] if dataset_code.startswith("CIP_") else dataset_code
        for entry in self.entries():
            if entry.gosterge_no == gosterge_no:
                return entry
        raise ConnectorError(NOT_FOUND, f"unknown CİP indicator {dataset_code!r}")

    def geometry(self, level: int) -> dict[str, GeometryUnit]:
        """The geo code -> label/parent map for one level (cached)."""
        if level in self._geometry:
            return self._geometry[level]
        with self._geometry_lock:
            if level in self._geometry:
                return self._geometry[level]
            if level == 1:
                units = {
                    code: GeometryUnit(code=code, label=label)
                    for code, label in IBBS1_LABELS.items()
                }
            else:
                response = self._client.geometry(level)
                units = parse_geometry(response.json())
            self._geometry[level] = units
            return units

    # --- catalog (datasets + dimension codelists; no values) ----------------

    def list_datasets(self) -> Iterator[DatasetMeta]:
        """Yield every sideMenu indicator as a dataset (failed ones flagged)."""
        for entry in self.entries():
            yield self._build_dataset(entry)

    def dataset_meta(self, dataset_code: str | CipEntry) -> DatasetMeta:
        """Full dataset metadata for one indicator, cataloguing every working level."""
        return self._build_dataset(self._resolve_entry(dataset_code))

    def _probe_level(self, entry: CipEntry, level: int) -> CipMapData:
        response = self._client.map_data(
            kaynak=entry.kaynak,
            duzey=level,
            gosterge_no=entry.gosterge_no,
            kayit_sayisi=CATALOG_KAYIT_SAYISI,
            period=entry.period,
        )
        return parse_map_data(response.json(), raw_object_key=response.raw_object_key)

    @staticmethod
    def _candidate_levels(entry: CipEntry) -> list[int]:
        """Levels to verify live for one indicator.

        Every level 1-4 is probed, whether or not the portal menu advertises it:
        the source is connected completely (user rule) and CİP is fast enough
        (~2000 req/min at 4 workers). Only levels that return data become codes.
        Level 5 never returns data and is never used.
        """
        return [1, 2, 3, 4]

    def _build_dataset(self, entry: CipEntry) -> DatasetMeta:
        probes: list[CipLevelProbe] = []
        errors: list[ConnectorError] = []
        for level in self._candidate_levels(entry):
            try:
                probes.append(CipLevelProbe(level=level, data=self._probe_level(entry, level)))
            except ConnectorError as exc:
                errors.append(exc)
                logger.info("cip catalog: %s level %d: %s", entry.gosterge_no, level, exc.kind)
        if not probes:
            reason = errors[0] if errors else ConnectorError(EMPTY, "no level returned data")
            self._record_failure(entry, reason)
            return self._flagged_dataset(entry, reason)

        return self._dataset_meta(entry, probes)

    def _flagged_dataset(self, entry: CipEntry, error: ConnectorError) -> DatasetMeta:
        return DatasetMeta(
            external_code=f"CIP_{entry.gosterge_no}",
            name=entry.name,
            description=entry.name_en,
            source_category=entry.menu_name,
            attributes={
                "channel": self.channel,
                "kaynak": entry.kaynak,
                "gosterge_no": entry.gosterge_no,
                "period": entry.period,
                "default_frequency": frequency_for_sdmx_code(frequency_code(entry.period)),
            },
            source_incomplete=True,
            source_incomplete_note=(
                f"no geographic level returned data ({error.kind}): {error.message}"
            ),
        )

    def _dataset_meta(self, entry: CipEntry, probes: list[CipLevelProbe]) -> DatasetMeta:
        working_levels = [probe.level for probe in probes]
        representative = probes[0].data
        first_decimals = representative.decimal_precision
        metadata_url = representative.metadata_url

        ref_codes: dict[str, DimensionCodeMeta] = {}
        coverage_start: date | None = None
        coverage_end: date | None = None
        obs_count = 0
        unmapped = 0
        for probe in probes:
            geo = self.geometry(probe.level)
            for unit in probe.data.units:
                if unit.code not in ref_codes:
                    geometry_unit = geo.get(unit.code)
                    if geometry_unit is None:
                        unmapped += 1
                        label = unit.code
                        parent = None
                    else:
                        label = geometry_unit.label
                        parent = self._parent_for_level(probe.level, geometry_unit)
                    ref_codes[unit.code] = DimensionCodeMeta(
                        code=unit.code, label=label, parent_code=parent
                    )
            valid_periods = 0
            for period in probe.data.periods:
                try:
                    start = period_start(period, probe.data.period)
                except ConnectorError:
                    # Monthly responses embed a "YYYY/0" annual marker; it is not a
                    # month and cannot be a monthly observation, so it is skipped.
                    continue
                coverage_start = start if coverage_start is None else min(coverage_start, start)
                coverage_end = start if coverage_end is None else max(coverage_end, start)
                valid_periods += 1
            obs_count += len(probe.data.units) * valid_periods

        level_codes = [
            DimensionCodeMeta(code=str(level), label=LEVEL_LABELS[level])
            for level in working_levels
        ]
        dimensions = [
            DimensionMeta(
                code=LEVEL_DIMENSION,
                label="Coğrafi düzey",
                position=0,
                role=ROLE_OTHER,
                codes=level_codes,
                attributes={"source_parameter": "duzey", "labels": LEVEL_LABELS},
            ),
            DimensionMeta(
                code=REF_AREA_DIMENSION,
                label="Coğrafi birim",
                position=1,
                role=ROLE_GEO,
                codes=list(ref_codes.values()),
                attributes={
                    "hierarchy_source": "source_geometri",
                    "note": (
                        "levels are mixed: level 3 codes are province plate codes, "
                        "level 4 codes are district codes; parents point at the İBBS "
                        "region (level 2)"
                    ),
                },
            ),
            DimensionMeta(code=TIME_DIMENSION, label="Dönem", position=2, role=ROLE_TIME),
        ]

        attributes: dict[str, Any] = {
            "channel": self.channel,
            "kaynak": entry.kaynak,
            "gosterge_no": entry.gosterge_no,
            "period": entry.period,
            "default_frequency": frequency_for_sdmx_code(frequency_code(entry.period)),
            "working_levels": working_levels,
            "ondalik_hassasiyet": first_decimals,
            "source_note": NULL_NOTE,
        }
        if metadata_url:
            attributes["meta_veri_url"] = metadata_url
        hint = unit_hint(representative.name or entry.name)
        if hint:
            attributes["unit_hint"] = hint
        if unmapped:
            attributes["unmapped_geo_codes"] = unmapped
        return DatasetMeta(
            external_code=f"CIP_{entry.gosterge_no}",
            name=representative.name or entry.name,
            description=entry.name_en,
            source_category=entry.menu_name,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            obs_count=obs_count,
            attributes=attributes,
            dimensions=dimensions,
        )

    @staticmethod
    def _parent_for_level(level: int, unit: GeometryUnit) -> str | None:
        if level == 1:
            return None
        if level == 2:
            return derive_nuts_parent(unit.code)
        # Levels 3 and 4: the geometry publishes the İBBS-2 region (bolgeKodu).
        return unit.region_code

    # --- fetch --------------------------------------------------------------

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        """Fetch one indicator/level/geo-unit time series (all available periods)."""
        entry = self._resolve_entry(dataset_code)
        level_raw = codes.get(LEVEL_DIMENSION)
        area = codes.get(REF_AREA_DIMENSION)
        if not level_raw or not area:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{dataset_code}: LEVEL and REF_AREA codes are required",
            )
        try:
            level = int(level_raw)
        except ValueError as exc:
            raise ConnectorError(FORMAT_CHANGED, f"invalid LEVEL {level_raw!r}") from exc
        if level not in SUPPORTED_LEVELS:
            raise ConnectorError(NOT_FOUND, f"unsupported CİP level {level}")

        response = self._client.map_data(
            kaynak=entry.kaynak,
            duzey=level,
            gosterge_no=entry.gosterge_no,
            kayit_sayisi=FETCH_KAYIT_SAYISI,
            period=entry.period,
        )
        data = parse_map_data(response.json(), raw_object_key=response.raw_object_key)
        unit = next((row for row in data.units if row.code == area), None)
        if unit is None:
            raise ConnectorError(
                EMPTY,
                f"{dataset_code}: geo unit {area!r} is not reported at level {level}",
                raw_object_key=response.raw_object_key,
            )
        points: list[tuple[date, Any]] = []
        for label, raw_value in zip(data.periods, unit.values, strict=False):
            try:
                period = period_start(label, data.period)
            except ConnectorError:
                # Monthly responses embed a "YYYY/0" annual marker; skip it.
                continue
            if period < start:
                continue
            points.append((period, parse_value(raw_value)))
        points.sort(key=lambda item: item[0])
        raw_keys = [key for key in (response.raw_object_key,) if key]
        return FetchResult(
            external_code=f"{dataset_code}:{level}.{area}",
            points=points,
            raw_object_keys=raw_keys,
            channel=self.channel,
        )


__all__ = [
    "CHANNEL",
    "CipCatalogFailure",
    "CipClient",
    "CipConnector",
    "CipEntry",
    "CipLevelProbe",
    "CipMapData",
    "CipUnit",
    "DEFAULT_BASE_URL",
    "GeometryUnit",
    "IBBS1_LABELS",
    "LEVEL_LABELS",
    "frequency_code",
    "is_error_payload",
    "parse_geometry",
    "parse_map_data",
    "parse_side_menu",
    "period_start",
    "unit_hint",
]
