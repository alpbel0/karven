"""Synchronous databrowser2 HTTP client.

Plain HTTP (httpx, sync) against ``https://databrowser2.tuik.gov.tr``:

- at most a small configured concurrency (callers own the pool),
- 30 s timeout per request, retries with growing backoff,
- an HTTP 200 ``text/html`` "Yönlendiriliyor..." page is throttling, not
  data: wait, retry, and mark the client so the run drops to one worker,
- every payload (success or offending error body) is stored in MinIO through
  :func:`app.connectors.base.store_raw`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    FORMAT_CHANGED,
    NOT_FOUND,
    SOURCE_ERROR,
    THROTTLED,
    TIMEOUT,
    ConnectorError,
    ObjectStore,
    store_raw,
)
from app.connectors.tuik.parsers import is_dataflow_not_found, is_throttle_response

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://databrowser2.tuik.gov.tr"
API_PREFIX = "/api/core/nodes/1"
CATALOG_CHANNEL = "databrowser2-catalog"
DATA_CHANNEL = "databrowser2-data"
CSV_CHANNEL = "databrowser2-csv"
STRUCTURE_CHANNEL = "databrowser2-structure"
CODELIST_CHANNEL = "databrowser2-codelist"
JSON_CHANNEL = "databrowser2-json"


@dataclass(frozen=True)
class RawResponse:
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
                "response body is not valid JSON",
                raw_object_key=self.raw_object_key,
            ) from exc


class Databrowser2Client:
    """Retrying, throttling-aware client for the databrowser2 JSON API."""

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
        self._timeout_s = resolved.tuik_request_timeout_s
        self._max_concurrency = resolved.tuik_max_concurrency
        self._max_retries = resolved.tuik_max_retries
        self._retry_backoff_s = resolved.tuik_retry_backoff_s
        self._throttle_wait_s = resolved.tuik_throttle_wait_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        self._throttled = False
        self._lock = threading.Lock()

    @property
    def throttled(self) -> bool:
        return self._throttled

    @property
    def max_concurrency(self) -> int:
        return 1 if self._throttled else self._max_concurrency

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _store_raw(self, dataset: str, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(self._institution, dataset, channel, payload, ext, store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("tuik raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    def _mark_throttled(self) -> None:
        with self._lock:
            self._throttled = True

    @staticmethod
    def _error_message(content: bytes) -> str:
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return content[:200].decode("utf-8", errors="replace")
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("errorCode") or payload)[:300]
        return str(payload)[:300]

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    def _request(
        self,
        method: str,
        path: str,
        *,
        dataset: str,
        channel: str,
        json_body: Any = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            try:
                response = self._client.request(method, url, json=json_body)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("tuik timeout on %s (attempt %d)", path, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR, f"transport error after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("tuik transport error on %s (attempt %d)", path, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            content_type = response.headers.get("content-type")
            # TÜİK answers very large datasets with 206 Partial Content and a
            # complete body; treat it like 200 (a truncated body then fails in
            # the JSON parser as format_changed).
            if response.status_code in (200, 206):
                if is_throttle_response(content_type, response.content):
                    key = self._store_raw(dataset, f"{channel}-throttle", response.content, "html")
                    self._mark_throttled()
                    if attempt >= self._max_retries:
                        raise ConnectorError(
                            THROTTLED,
                            "TÜİK returned a throttle (Yönlendiriliyor) page",
                            raw_object_key=key,
                        )
                    wait = self._throttle_wait_s * (2**attempt)
                    logger.warning(
                        "tuik throttle page on %s; waiting %.1fs (attempt %d)",
                        path,
                        wait,
                        attempt + 1,
                    )
                    self._sleeper(wait)
                    attempt += 1
                    continue
                return response

            if response.status_code == 500 and is_dataflow_not_found(response.content):
                key = self._store_raw(dataset, f"{channel}-error", response.content, "json")
                raise ConnectorError(
                    NOT_FOUND,
                    f"DATAFLOW_NOT_FOUND: {self._error_message(response.content)}",
                    raw_object_key=key,
                )
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
                    "tuik HTTP %d on %s (attempt %d)",
                    response.status_code,
                    path,
                    attempt + 1,
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

    def catalog(self) -> RawResponse:
        response = self._request(
            "GET", f"{API_PREFIX}/catalog", dataset="catalog", channel=CATALOG_CHANNEL
        )
        key = self._store_raw("catalog", CATALOG_CHANNEL, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def dataset_data(self, dataset_id: str, criteria: list[dict[str, Any]]) -> RawResponse:
        path = f"{API_PREFIX}/datasets/{dataset_id}/data"
        response = self._request(
            "POST", path, dataset=dataset_id, channel=DATA_CHANNEL, json_body=criteria
        )
        key = self._store_raw(dataset_id, DATA_CHANNEL, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def dataset_csv(self, dataset_id: str, criteria: list[dict[str, Any]]) -> RawResponse:
        path = f"{API_PREFIX}/datasets/{dataset_id}/download/csv"
        response = self._request(
            "POST", path, dataset=dataset_id, channel=CSV_CHANNEL, json_body=criteria
        )
        key = self._store_raw(dataset_id, CSV_CHANNEL, response.content, "csv")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def dataset_download_json(self, dataset_id: str, criteria: list[dict[str, Any]]) -> RawResponse:
        """Full SDMX-JSON data message (all time periods), unlike the view-bound ``/data``."""
        path = f"{API_PREFIX}/datasets/{dataset_id}/download/json"
        response = self._request(
            "POST", path, dataset=dataset_id, channel=JSON_CHANNEL, json_body=criteria
        )
        key = self._store_raw(dataset_id, JSON_CHANNEL, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def dataset_structure(self, dataset_id: str) -> RawResponse:
        path = f"{API_PREFIX}/datasets/{dataset_id}/structure"
        response = self._request("GET", path, dataset=dataset_id, channel=STRUCTURE_CHANNEL)
        key = self._store_raw(dataset_id, STRUCTURE_CHANNEL, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def dataset_partial_codelist(
        self,
        dataset_id: str,
        dimension: str,
        criteria: list[dict[str, Any]] | None = None,
    ) -> RawResponse:
        path = f"{API_PREFIX}/datasets/{dataset_id}/PartialCodelists/{dimension}"
        response = self._request(
            "POST",
            path,
            dataset=dataset_id,
            channel=CODELIST_CHANNEL,
            json_body=criteria if criteria is not None else [],
        )
        key = self._store_raw(dataset_id, CODELIST_CHANNEL, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)
