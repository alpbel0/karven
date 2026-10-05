"""TÜİK nsiws SDMX REST channel — the official backup series fetcher.

databrowser2 (token-free JSON-stat) stays the primary channel; this module is
the backup used by :class:`~app.connectors.tuik.connector.TuikConnector` when a
databrowser2 fetch fails with ``timeout``, ``source_error``, ``throttled`` or
``not_found``. The same series (same dataset + codes) is then requested from the
official SDMX 2.1 REST endpoint ``nsiws.tuik.gov.tr`` and returns
``channel="nsiws"``.

Auth (measured 2026-09-09): the ``TUIK_API_KEY`` is exchanged for a ~5 minute
JWT at the Keycloak token endpoint; the token is cached, refreshed one minute
before expiry and refreshed once more on a 401. Without a key the channel is
disabled (a single INFO log line, never a crash).

Data: ``GET /rest/data/TR,<DATAFLOW>,<VERSION>/<key>?startPeriod=...`` returns
SDMX 2.1 generic XML (the endpoint ignores ``format=JSON``/``SDMX-CSV``). The
key must contain one value per DSD dimension: dimensions present in the series'
codes use their code, the remaining dimensions use the ``%20`` wildcard, which
nsiws resolves to the same default the databrowser2 view applies. Those remaining
dimensions are usually view settings, but a dataflow's hidden dimensions may
still be real data dimensions with codes in the catalogued series key; when a
code is present ``build_key`` uses it, and only genuinely uncoded dimensions are
wildcarded. The DSD dimension order is discovered once per dataflow and cached.

Measured 2026-09-30: nsiws drops ~25% of requests to a silent 30 s timeout at a
deterministic cadence; retries with backoff recover them.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    SOURCE_ERROR,
    THROTTLED,
    TIMEOUT,
    ConnectorError,
    FetchResult,
    ObjectStore,
    store_raw,
)
from app.connectors.tuik.parsers import parse_period_start, parse_value
from app.data.periods import frequency_for_sdmx_code

logger = logging.getLogger(__name__)

CHANNEL = "nsiws"
DATA_CHANNEL = "nsiws-data"
DATAFLOW_CHANNEL = "nsiws-dataflow"
STRUCTURE_CHANNEL = "nsiws-structure"

DEFAULT_BASE_URL = "https://nsiws.tuik.gov.tr/rest"
DEFAULT_TOKEN_URL = "https://giris.tuik.gov.tr/realms/web/protocol/openid-connect/token"
DEFAULT_CLIENT_ID = "nsi-ws-consumer"

AGENCY = "TR"
TIME_DIMENSION = "TIME_PERIOD"

# The SDMX dimension wildcard nsiws accepts (a blank value encoded as a space);
# a literal empty segment is rejected by the fronting IIS as a bad path.
WILDCARD = "%20"

# databrowser2 failure kinds that trigger the nsiws backup.
FALLBACK_KINDS = frozenset({TIMEOUT, SOURCE_ERROR, THROTTLED, NOT_FOUND})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class NsiwsTokenManager:
    """Caches the short-lived nsiws JWT and refreshes it before expiry."""

    def __init__(
        self,
        *,
        token_url: str = DEFAULT_TOKEN_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
        refresh_skew_s: float = 60.0,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._api_key = api_key or ""
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client()
        self._refresh_skew_s = refresh_skew_s
        self._sleeper = sleeper
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def token_url(self) -> str:
        return self._token_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def token(self, *, force: bool = False) -> str | None:
        """Return a valid access token, fetching one when needed."""
        if not self.configured:
            return None
        with self._lock:
            now = self._clock()
            if not force and self._token is not None and now < self._expires_at:
                return self._token
            return self._fetch(now)

    def _fetch(self, now: float) -> str:
        payload = {
            "grant_type": "password",
            "client_id": self._client_id,
            "api_key": self._api_key,
        }
        try:
            response = self._client.post(self._token_url, data=payload)
        except httpx.TimeoutException as exc:
            raise ConnectorError(TIMEOUT, f"nsiws token request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise ConnectorError(SOURCE_ERROR, f"nsiws token transport error: {exc}") from exc
        if response.status_code != 200:
            raise ConnectorError(
                SOURCE_ERROR,
                f"nsiws token endpoint returned HTTP {response.status_code}",
            )
        try:
            body = response.json()
            access_token = str(body["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ConnectorError(
                FORMAT_CHANGED, "nsiws token response has no access_token"
            ) from exc
        try:
            expires_in = float(body.get("expires_in") or 300)
        except (TypeError, ValueError):
            expires_in = 300.0
        self._token = access_token
        self._expires_at = now + max(0.0, expires_in - self._refresh_skew_s)
        return access_token


def _encode(value: str) -> str:
    return quote(value, safe="")


def period_param(frequency: str | None, value: date) -> str:
    """Format ``startPeriod`` for the SDMX frequency (year/month/quarter/day)."""
    if frequency == "monthly":
        return f"{value.year:04d}-{value.month:02d}"
    if frequency == "quarterly":
        return f"{value.year:04d}-Q{(value.month - 1) // 3 + 1}"
    if frequency == "semiannual":
        return f"{value.year:04d}-S{1 if value.month <= 6 else 2}"
    if frequency in ("daily", "weekly"):
        return value.isoformat()
    if frequency in ("annual", "biennial"):
        return f"{value.year:04d}"
    return f"{value.year:04d}-{value.month:02d}"


def parse_generic_data(payload: bytes, key: dict[str, str]) -> list[tuple[date, Decimal | None]]:
    """Extract the points whose series key matches every ``key`` dimension.

    nsiws returns SDMX 2.1 generic XML. If wildcarded dimensions match more than
    one series the request is ambiguous: raise instead of guessing which one the
    caller meant (a wrong series would silently corrupt the data).
    """
    text = payload.strip()
    if not text.startswith(b"<"):
        raise ConnectorError(FORMAT_CHANGED, "nsiws response is not SDMX-ML XML")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ConnectorError(FORMAT_CHANGED, f"nsiws returned malformed XML: {exc}") from exc

    data_set = None
    for element in root.iter():
        if _local(element.tag) == "DataSet":
            data_set = element
            break
    if data_set is None:
        raise ConnectorError(FORMAT_CHANGED, "nsiws XML has no DataSet element")

    matches: list[list[tuple[date, Decimal | None]]] = []
    for series in data_set:
        if _local(series.tag) != "Series":
            continue
        series_key: dict[str, str] = {}
        for value in series:
            if _local(value.tag) == "SeriesKey":
                for item in value:
                    if _local(item.tag) == "Value" and item.get("id") is not None:
                        series_key[str(item.get("id"))] = str(item.get("value") or "")
        if any(series_key.get(dim) != code for dim, code in key.items()):
            continue
        points: list[tuple[date, Decimal | None]] = []
        for obs in series:
            if _local(obs.tag) != "Obs":
                continue
            period_raw: str | None = None
            value_raw: str | None = None
            for item in obs:
                name = _local(item.tag)
                if name == "ObsDimension" and item.get("id") in (None, TIME_DIMENSION):
                    period_raw = item.get("value")
                elif name == "ObsValue":
                    value_raw = item.get("value")
            if period_raw is None:
                continue
            points.append((parse_period_start(period_raw), parse_value(value_raw)))
        points.sort(key=lambda item: item[0])
        matches.append(points)

    if not matches:
        raise ConnectorError(EMPTY, "nsiws response has no series matching the requested key")
    if len(matches) > 1:
        raise ConnectorError(
            FORMAT_CHANGED,
            f"nsiws key is ambiguous: {len(matches)} series match {key}; refusing to guess",
        )
    return matches[0]


@dataclass(frozen=True)
class RawResponse:
    """One successful nsiws HTTP response plus where its body was stored."""

    status_code: int
    content: bytes
    raw_object_key: str | None

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConnectorError(FORMAT_CHANGED, "nsiws response body is not valid JSON") from exc


class NsiwsClient:
    """SDMX REST client for the official TÜİK nsiws service (backup channel)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token_url: str | None = None,
        client_id: str | None = None,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = "tuik",
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved = settings_obj or global_settings
        if api_key is None and resolved.tuik_api_key is not None:
            api_key = resolved.tuik_api_key.get_secret_value()
        self._base_url = (base_url or resolved.tuik_nsiws_base_url).rstrip("/")
        self._timeout_s = resolved.tuik_nsiws_request_timeout_s
        self._max_retries = resolved.tuik_nsiws_max_retries
        self._retry_backoff_s = resolved.tuik_nsiws_retry_backoff_s
        self._refresh_skew_s = resolved.tuik_nsiws_token_refresh_skew_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._clock = clock
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout_s)
        self._tokens = NsiwsTokenManager(
            token_url=token_url or resolved.tuik_nsiws_token_url,
            client_id=client_id or resolved.tuik_nsiws_client_id,
            api_key=api_key,
            http_client=self._client,
            refresh_skew_s=self._refresh_skew_s,
            sleeper=sleeper,
            clock=clock,
        )
        self._dimension_orders: dict[tuple[str, str], list[str]] = {}
        self._structures: dict[tuple[str, str], tuple[str, str]] = {}

    @property
    def configured(self) -> bool:
        return self._tokens.configured

    @property
    def tokens(self) -> NsiwsTokenManager:
        return self._tokens

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- raw storage ------------------------------------------------------

    def _store_raw(self, dataset: str, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(self._institution, dataset, channel, payload, ext, store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("nsiws raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    # --- HTTP -------------------------------------------------------------

    def _request(
        self,
        path: str,
        *,
        dataset: str,
        channel: str,
        ext: str = "xml",
        error_ext: str = "txt",
        retried_auth: bool = False,
    ) -> RawResponse:
        if not self.configured:
            raise ConnectorError(
                SOURCE_ERROR, "nsiws channel is not configured (TUIK_API_KEY empty)"
            )
        url = f"{self._base_url}{path}"
        token = self._tokens.token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        attempt = 0
        while True:
            try:
                response = self._client.get(url, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"nsiws timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("nsiws timeout on %s (attempt %d)", channel, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR, f"nsiws transport error after {attempt + 1} attempts: {exc}"
                    ) from exc
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            status = response.status_code
            if status == 200:
                key = self._store_raw(dataset, channel, response.content, ext)
                return RawResponse(status, response.content, key)
            if status == 401 and not retried_auth:
                logger.warning("nsiws 401 on %s; refreshing token once", channel)
                self._tokens.invalidate()
                return self._request(
                    path,
                    dataset=dataset,
                    channel=channel,
                    ext=ext,
                    error_ext=error_ext,
                    retried_auth=True,
                )
            if status == 404:
                key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
                raise ConnectorError(
                    NOT_FOUND,
                    f"nsiws HTTP 404: {_short(response.content, response)}",
                    raw_object_key=key,
                )
            if status == 422:
                key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
                raise ConnectorError(
                    FORMAT_CHANGED,
                    f"nsiws HTTP 422: {_short(response.content, response)}",
                    raw_object_key=key,
                )
            if status == 401:
                key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
                raise ConnectorError(
                    SOURCE_ERROR,
                    "nsiws still unauthorized after a token refresh",
                    raw_object_key=key,
                )
            if status == 429 or status >= 500:
                if attempt >= self._max_retries:
                    key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"nsiws HTTP {status}: {_short(response.content, response)}",
                        raw_object_key=key,
                    )
                logger.warning("nsiws HTTP %d on %s (attempt %d)", status, channel, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
            raise ConnectorError(
                SOURCE_ERROR,
                f"nsiws HTTP {status}: {_short(response.content, response)}",
                raw_object_key=key,
            )

    # --- metadata ---------------------------------------------------------

    def structure_ref(self, dataflow_code: str, version: str) -> tuple[str, str]:
        """Return ``(dsd_id, dsd_version)`` for a dataflow, caching the answer."""
        cache_key = (dataflow_code, version)
        cached = self._structures.get(cache_key)
        if cached is not None:
            return cached
        path = f"/dataflow/{AGENCY}/{_encode(dataflow_code)}/{_encode(version)}"
        response = self._request(path, dataset=dataflow_code, channel=DATAFLOW_CHANNEL)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"nsiws dataflow response for {dataflow_code} is malformed XML",
                raw_object_key=response.raw_object_key,
            ) from exc
        for element in root.iter():
            if _local(element.tag) != "Ref":
                continue
            ref_id = element.get("id") or ""
            if ref_id.startswith("DSD_"):
                ref_version = element.get("version")
                if not ref_version:
                    raise ConnectorError(
                        FORMAT_CHANGED,
                        f"nsiws dataflow {dataflow_code} DSD ref has no version",
                        raw_object_key=response.raw_object_key,
                    )
                resolved = (ref_id, str(ref_version))
                self._structures[cache_key] = resolved
                return resolved
        raise ConnectorError(
            FORMAT_CHANGED,
            f"nsiws dataflow {dataflow_code} exposes no DSD reference",
            raw_object_key=response.raw_object_key,
        )

    def dimension_order(self, dataflow_code: str, version: str) -> list[str]:
        """DSD dimension ids (without TIME_PERIOD) in positional order."""
        cache_key = (dataflow_code, version)
        cached = self._dimension_orders.get(cache_key)
        if cached is not None:
            return cached
        dsd_id, dsd_version = self.structure_ref(dataflow_code, version)
        path = f"/datastructure/{AGENCY}/{_encode(dsd_id)}/{_encode(dsd_version)}"
        response = self._request(path, dataset=dataflow_code, channel=STRUCTURE_CHANNEL)
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"nsiws DSD {dsd_id} is malformed XML",
                raw_object_key=response.raw_object_key,
            ) from exc
        positioned: list[tuple[int, str]] = []
        for element in root.iter():
            if _local(element.tag) != "Dimension":
                continue
            dim_id = element.get("id")
            position = element.get("position")
            if not dim_id or position is None:
                continue
            if dim_id == TIME_DIMENSION:
                continue
            try:
                positioned.append((int(position), str(dim_id)))
            except ValueError:
                continue
        if not positioned:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"nsiws DSD {dsd_id} exposes no dimensions",
                raw_object_key=response.raw_object_key,
            )
        positioned.sort(key=lambda item: item[0])
        order = [dim_id for _, dim_id in positioned]
        self._dimension_orders[cache_key] = order
        return order

    # --- data -------------------------------------------------------------

    def build_key(self, dimension_order: list[str], codes: dict[str, str]) -> str:
        """Positional SDMX key; missing dimensions become the ``%20`` wildcard."""
        parts = []
        for dimension in dimension_order:
            code = codes.get(dimension)
            parts.append(_encode(code) if code else WILDCARD)
        return ".".join(parts)

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        version: str,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        """Fetch one series through nsiws and return its points."""
        dimensions = self.dimension_order(dataset_code, version)
        unknown = sorted(dim for dim in codes if dim not in dimensions)
        if unknown:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{dataset_code}: codes for unknown nsiws dimensions {unknown}",
            )
        key = self.build_key(dimensions, codes)
        frequency = _frequency_for_codes(codes)
        start_period = period_param(frequency, start)
        path = (
            f"/data/{AGENCY},{_encode(dataset_code)},{_encode(version)}/"
            f"{key}?startPeriod={_encode(start_period)}"
        )
        response = self._request(path, dataset=dataset_code, channel=DATA_CHANNEL)
        points = parse_generic_data(response.content, codes)
        points = [point for point in points if point[0] >= start]
        if not points:
            raise ConnectorError(
                EMPTY,
                f"{dataset_code}: nsiws returned no observations",
                raw_object_key=response.raw_object_key,
            )
        ordered = order or list(codes)
        external_code = f"{dataset_code}:" + ".".join(codes[dim] for dim in ordered)
        keys = [key_ for key_ in (response.raw_object_key,) if key_]
        return FetchResult(
            external_code=external_code,
            points=points,
            raw_object_keys=keys,
            channel=CHANNEL,
        )


def _frequency_for_codes(codes: dict[str, str]) -> str | None:
    raw = codes.get("FREQ")
    if raw is None:
        return None
    try:
        return frequency_for_sdmx_code(raw)
    except Exception:  # noqa: BLE001 - an unknown FREQ only affects startPeriod shape
        return None


def _short(content: bytes, response: httpx.Response) -> str:
    if not content:
        return f"HTTP {response.status_code}"
    return content[:200].decode("utf-8", errors="replace")


# --- parity ---------------------------------------------------------------


@dataclass(frozen=True)
class ParityReport:
    """Difference between two point lists for verification helpers."""

    left_only: tuple[date, ...]
    right_only: tuple[date, ...]
    value_mismatches: tuple[tuple[date, Decimal | None, Decimal | None], ...]

    @property
    def matching(self) -> bool:
        return not (self.left_only or self.right_only or self.value_mismatches)


def compare_points(
    left: list[tuple[date, Decimal | None]],
    right: list[tuple[date, Decimal | None]],
) -> ParityReport:
    """Compare two period->value point lists (order-insensitive)."""
    left_map = dict(left)
    right_map = dict(right)
    left_only = tuple(sorted(set(left_map) - set(right_map)))
    right_only = tuple(sorted(set(right_map) - set(left_map)))
    mismatches = tuple(
        sorted(
            (period, left_map[period], right_map[period])
            for period in set(left_map) & set(right_map)
            if left_map[period] != right_map[period]
        )
    )
    return ParityReport(left_only=left_only, right_only=right_only, value_mismatches=mismatches)


def build_nsiws_client(
    *,
    store: ObjectStore | None = None,
    settings_obj: Settings | None = None,
    **kwargs: Any,
) -> NsiwsClient | None:
    """Return an enabled nsiws client, or ``None`` when no API key is configured."""
    resolved = settings_obj or global_settings
    if resolved.tuik_api_key is None or not resolved.tuik_api_key.get_secret_value():
        logger.info("tuik nsiws backup disabled: TUIK_API_KEY is empty")
        return None
    return NsiwsClient(store=store, settings_obj=resolved, **kwargs)


__all__ = [
    "CHANNEL",
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_TOKEN_URL",
    "FALLBACK_KINDS",
    "NsiwsClient",
    "NsiwsTokenManager",
    "ParityReport",
    "build_nsiws_client",
    "compare_points",
    "parse_generic_data",
    "period_param",
]
