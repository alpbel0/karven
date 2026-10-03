"""Synchronous EVDS3 HTTP client (``https://evds3.tcmb.gov.tr/igmevdsms-dis``).

Key-less plain JSON over httpx (sync):

- one worker, at least ``tcmb_request_interval_s`` between consecutive requests,
- retries with growing backoff on timeouts/transport errors and 5xx/429,
- deterministic 400/500 HTML errors are NOT retried (the source uses them for a
  bad series or date, indistinguishable from each other),
- every payload (success or offending error body) is stored through
  :func:`app.connectors.base.store_raw`; a storage failure never breaks the data
  path.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

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

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://evds3.tcmb.gov.tr/igmevdsms-dis"
CATALOG_CHANNEL = "evds3-catalog"
SERIELIST_CHANNEL = "evds3-serielist"
BOUNDS_CHANNEL = "evds3-bounds"
DATA_CHANNEL = "evds3-data"


@dataclass(frozen=True)
class EvdsResponse:
    """One successful HTTP response plus where its body was stored."""

    status_code: int
    content: bytes
    headers: Mapping[str, str]
    raw_object_key: str | None

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConnectorError(
                FORMAT_CHANGED,
                "EVDS3 response body is not valid JSON",
                raw_object_key=self.raw_object_key,
            ) from exc


class EvdsClient:
    """Retrying, paced, raw-storing client for the EVDS3 JSON API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = "tcmb",
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = (base_url or resolved.tcmb_base_url).rstrip("/")
        self._interval_s = resolved.tcmb_request_interval_s
        self._timeout_s = resolved.tcmb_request_timeout_s
        self._max_retries = resolved.tcmb_max_retries
        self._retry_backoff_s = resolved.tcmb_retry_backoff_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._clock = clock
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        self._last_request_at: float | None = None
        # Plain counters the CLI and tests can read.
        self.requests = 0
        self.retries = 0
        self.throttled = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _store_raw(self, dataset: str, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(self._institution, dataset, channel, payload, ext, store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("tcmb raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    def _pace(self) -> None:
        if self._last_request_at is not None:
            wait = self._interval_s - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleeper(wait)
        self._last_request_at = self._clock()

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    @staticmethod
    def _ext_for(response: httpx.Response) -> str:
        content_type = response.headers.get("content-type", "")
        return "html" if "html" in content_type.lower() else "json"

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int, backoff: float) -> float:
        header = response.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return backoff * (2**attempt)

    @staticmethod
    def _error_message(content: bytes) -> str:
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return content[:200].decode("utf-8", errors="replace")
        return str(payload)[:300]

    def _request(
        self,
        method: str,
        path: str,
        *,
        dataset: str,
        channel: str,
        json_body: Any = None,
    ) -> httpx.Response:
        url = f"{self._base_url}/{path}"
        headers = (
            {"Content-Type": "application/json", "Accept": "application/json"}
            if json_body is not None
            else {"Accept": "application/json"}
        )
        attempt = 0
        while True:
            self._pace()
            self.requests += 1
            try:
                response = self._client.request(method, url, headers=headers, json=json_body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"EVDS3 {path} failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("tcmb %s on %s (attempt %d)", type(exc).__name__, path, attempt + 1)
                self.retries += 1
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            if response.status_code == 200:
                return response

            key = self._store_raw(
                dataset, f"{channel}-error", response.content, self._ext_for(response)
            )
            if response.status_code == 429:
                self.throttled += 1
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        THROTTLED,
                        f"EVDS3 throttled on {path} after {attempt + 1} attempts",
                        raw_object_key=key,
                    )
                wait = self._retry_after(response, attempt, self._retry_backoff_s)
                logger.warning("tcmb throttled on %s; waiting %.1fs", path, wait)
                self.retries += 1
                self._sleeper(wait)
                attempt += 1
                continue
            if response.status_code in (502, 503, 504):
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"EVDS3 HTTP {response.status_code} on {path}: "
                        f"{self._error_message(response.content)}",
                        raw_object_key=key,
                    )
                logger.warning(
                    "tcmb HTTP %d on %s (attempt %d)", response.status_code, path, attempt + 1
                )
                self.retries += 1
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            # 400/500 are deterministic HTML errors for a bad series/date: no retry.
            raise ConnectorError(
                SOURCE_ERROR,
                f"EVDS3 HTTP {response.status_code} on {path}: "
                f"{self._error_message(response.content)}",
                raw_object_key=key,
            )

    def _success(self, response: httpx.Response, dataset: str, channel: str) -> EvdsResponse:
        key = self._store_raw(dataset, channel, response.content, self._ext_for(response))
        return EvdsResponse(response.status_code, response.content, dict(response.headers), key)

    def get_catalog(self) -> EvdsResponse:
        response = self._request(
            "GET",
            "categories/withDatagroups/type=json",
            dataset="catalog",
            channel=CATALOG_CHANNEL,
        )
        return self._success(response, "catalog", CATALOG_CHANNEL)

    def get_serie_list(self, group: str) -> EvdsResponse:
        response = self._request(
            "GET",
            f"serieList/fe/type=json&code={group}",
            dataset=group,
            channel=SERIELIST_CHANNEL,
        )
        return self._success(response, group, SERIELIST_CHANNEL)

    def get_bounds(self, serie_code: str, *, group: str = "_series") -> EvdsResponse:
        """``group`` only names the raw-storage folder (the datagroup the series is in)."""
        body = {"frequency": 1, "series": [serie_code], "datagroups": [None]}
        response = self._request(
            "POST",
            "serieList/baslangicBitis",
            dataset=group,
            channel=BOUNDS_CHANNEL,
            json_body=body,
        )
        return self._success(response, group, BOUNDS_CHANNEL)

    def get_data(self, body: dict[str, Any], *, group: str = "_series") -> EvdsResponse:
        response = self._request("POST", "fe", dataset=group, channel=DATA_CHANNEL, json_body=body)
        return self._success(response, group, DATA_CHANNEL)


__all__ = [
    "BOUNDS_CHANNEL",
    "CATALOG_CHANNEL",
    "DATA_CHANNEL",
    "DEFAULT_BASE_URL",
    "SERIELIST_CHANNEL",
    "EvdsClient",
    "EvdsResponse",
]
