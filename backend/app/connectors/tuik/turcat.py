"""TÜİK Turcat (IMF SDDS national summary data page) connector.

Five token-free sector endpoints expose the latest and previous value of the
country's key macro indicators. Each sector becomes one catalog dataset whose
single ``INDICATOR`` dimension carries one code per Turcat row (the source's own
``ID``). Values are ingested as observations through the shared catalog helpers,
so daily polling (Task 1.4) accumulates a first-published history even though the
source only ever shows the two most recent periods.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    FORMAT_CHANGED,
    NOT_FOUND,
    ROLE_OTHER,
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
    ensure_series,
    store_raw,
)
from app.connectors.tuik.turcat_parsers import (
    SECTOR_BY_EXTERNAL_CODE,
    SECTORS,
    SectorParse,
    parse_sector,
    parse_turcat_payload,
)
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset
from app.data.observations import record_observations

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.tuik.gov.tr"
CHANNEL = "turcat"
INSTITUTION = "tuik"
INSTITUTION_NAME = "Türkiye İstatistik Kurumu"


@dataclass(frozen=True)
class TurcatResponse:
    """One successful Turcat HTTP response plus where its body was stored."""

    status_code: int
    content: bytes
    raw_object_key: str | None


class TurcatClient:
    """Small retrying HTTP client for the five Turcat sector endpoints."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = INSTITUTION,
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = base_url.rstrip("/")
        self._timeout_s = resolved.turcat_request_timeout_s
        self._max_concurrency = resolved.turcat_max_concurrency
        self._max_retries = resolved.turcat_max_retries
        self._retry_backoff_s = resolved.turcat_retry_backoff_s
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
            logger.warning("turcat raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    def sector(self, page_key: str) -> TurcatResponse:
        """GET one sector endpoint (``Reel``/``Mali``/``Finans``/``Dis``/``Nufus``)."""
        path = f"/Turcat/ViewTurcatPage{page_key}"
        url = f"{self._base_url}{path}"
        channel = f"turcat-{page_key}"
        attempt = 0
        while True:
            try:
                response = self._client.get(url)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"Turcat {page_key} timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                self._sleeper(self._retry_backoff_s * (2**attempt))
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"Turcat {page_key} transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                self._sleeper(self._retry_backoff_s * (2**attempt))
                attempt += 1
                continue

            if 200 <= response.status_code < 300:
                key = self._store_raw(page_key, channel, response.content, "json")
                return TurcatResponse(response.status_code, response.content, key)
            if response.status_code in (404, 410):
                raise ConnectorError(NOT_FOUND, f"Turcat {page_key} HTTP {response.status_code}")
            if response.status_code >= 500 and attempt < self._max_retries:
                self._sleeper(self._retry_backoff_s * (2**attempt))
                attempt += 1
                continue
            body = response.content[:200].decode("utf-8", errors="replace")
            raise ConnectorError(
                SOURCE_ERROR, f"Turcat {page_key} HTTP {response.status_code}: {body}"
            )


@dataclass(frozen=True)
class SectorFetch:
    """One sector fetch with its parsed rows and the raw payload key."""

    parse: SectorParse
    raw_object_key: str | None


class TurcatConnector(SourceConnector):
    """Lists and fetches the five Turcat sector datasets."""

    institution_code = INSTITUTION
    institution_name = INSTITUTION_NAME
    channel = CHANNEL

    def __init__(
        self,
        *,
        client: TurcatClient | None = None,
        store: ObjectStore | None = None,
        http_client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        settings_obj: Settings | None = None,
    ) -> None:
        if client is None:
            if store is None:
                store = MinioObjectStore(settings_obj)
            client = TurcatClient(
                base_url=base_url,
                http_client=http_client,
                store=store,
                institution=self.institution_code,
                settings_obj=settings_obj,
            )
        self._client = client
        self._cache: dict[str, SectorFetch] = {}

    @property
    def client(self) -> TurcatClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def fetch_sector(self, external_code: str, *, refresh: bool = False) -> SectorFetch:
        """Fetch and parse one sector, caching within the run."""
        if external_code not in SECTOR_BY_EXTERNAL_CODE:
            raise ConnectorError(NOT_FOUND, f"unknown Turcat dataset {external_code!r}")
        if not refresh and external_code in self._cache:
            return self._cache[external_code]
        page_key = SECTOR_BY_EXTERNAL_CODE[external_code][0]
        response = self._client.sector(page_key)
        parsed = parse_sector(response.content, external_code)
        fetch = SectorFetch(parse=parsed, raw_object_key=response.raw_object_key)
        self._cache[external_code] = fetch
        return fetch

    # --- catalog -----------------------------------------------------------

    def list_datasets(self) -> Iterator[DatasetMeta]:
        """Yield the five sector datasets with their full INDICATOR codelists."""
        for external_code, page_key, page, page_id in SECTORS:
            try:
                fetch = self.fetch_sector(external_code)
            except ConnectorError as exc:
                logger.warning("turcat catalog: %s failed (%s): %s", external_code, exc.kind, exc)
                continue
            yield self.dataset_meta_from(fetch, external_code, page_key, page, page_id)

    def dataset_meta(self, external_code: str) -> DatasetMeta:
        """Metadata for one sector dataset (fetches it if not cached)."""
        if external_code not in SECTOR_BY_EXTERNAL_CODE:
            raise ConnectorError(NOT_FOUND, f"unknown Turcat dataset {external_code!r}")
        page_key, page, page_id = SECTOR_BY_EXTERNAL_CODE[external_code]
        return self.dataset_meta_from(
            self.fetch_sector(external_code), external_code, page_key, page, page_id
        )

    @staticmethod
    def dataset_meta_from(
        fetch: SectorFetch, external_code: str, page_key: str, page: str, page_id: int
    ) -> DatasetMeta:
        parsed = fetch.parse
        codes: list[DimensionCodeMeta] = []
        for group in parsed.groups:
            attributes: dict[str, Any] = {"group": True, "page": page}
            if group.meta_url:
                attributes["meta_url"] = group.meta_url
            codes.append(
                DimensionCodeMeta(
                    code=group.code,
                    label=group.name,
                    parent_code=group.parent_code,
                    attributes=attributes,
                )
            )
        periods: list[date] = []
        obs_count = 0
        for indicator in parsed.indicators:
            attributes = {"page": page}
            if indicator.frequency:
                attributes["frequency"] = indicator.frequency
            if indicator.unit:
                attributes["unit"] = indicator.unit
            if indicator.meta_url:
                attributes["meta_url"] = indicator.meta_url
            if indicator.data_url:
                attributes["data_url"] = indicator.data_url
            codes.append(
                DimensionCodeMeta(
                    code=indicator.code,
                    label=indicator.name,
                    parent_code=indicator.parent_code,
                    attributes=attributes,
                )
            )
            if indicator.period is not None and indicator.latest_value is not None:
                periods.append(indicator.period)
                obs_count += 1

        dimension = DimensionMeta(
            code="INDICATOR",
            label="Gösterge",
            position=0,
            role=ROLE_OTHER,
            codes=codes,
            attributes={"page": page, "source": "IMF SDDS"},
        )
        return DatasetMeta(
            external_code=external_code,
            name=f"TÜİK Turcat — {page} (IMF SDDS)",
            description=(
                "IMF SDDS Ulusal Veri Yayımlama Sayfası: her göstergenin en güncel "
                "ve bir önceki dönem değeri."
            ),
            source_category=f"Turcat / {page}",
            coverage_start=min(periods) if periods else None,
            coverage_end=max(periods) if periods else None,
            obs_count=obs_count,
            attributes={
                "channel": CHANNEL,
                "sector": page,
                "sector_key": page_key,
                "page_id": page_id,
                "endpoint": f"{DEFAULT_BASE_URL}/Turcat/ViewTurcatPage{page_key}",
                "scheme": "IMF SDDS",
            },
            dimensions=[dimension],
        )

    # --- values ------------------------------------------------------------

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        """Return the latest and previous observations of one indicator."""
        code = codes.get("INDICATOR")
        if code is None:
            raise ConnectorError(
                FORMAT_CHANGED, f"{dataset_code}: missing INDICATOR code in {sorted(codes)}"
            )
        fetch = self.fetch_sector(dataset_code)
        indicator = next(
            (item for item in fetch.parse.indicators if item.code == code),
            None,
        )
        if indicator is None:
            raise ConnectorError(NOT_FOUND, f"{dataset_code}: no indicator with code {code!r}")
        points = [point for point in indicator.points if point[0] >= start]
        return FetchResult(
            external_code=f"{dataset_code}:{code}",
            points=points,
            raw_object_keys=[fetch.raw_object_key] if fetch.raw_object_key else [],
            channel=self.channel,
        )


def parse_turcat_response(content: bytes) -> list[dict[str, Any]]:
    """Backwards-friendly helper: decode a raw body into row dicts."""
    return parse_turcat_payload(content)


@dataclass(frozen=True)
class SectorIngest:
    """Outcome of ingesting one sector's rows as observations."""

    indicators: int
    series: int
    points: int
    inserted: int
    unchanged: int
    skipped: int


def ingest_sector(
    session: Session,
    connector: TurcatConnector,
    dataset: Dataset,
    fetch: SectorFetch,
    *,
    start: date = date(2000, 1, 1),
    fetched_at: datetime | None = None,
) -> SectorIngest:
    """Create the needed series and append the latest/previous observations.

    Series rows are created lazily through :func:`ensure_series`; observations go
    through the shared append-only writer, so unchanged values are not rewritten
    and the first-published value per period accumulates over time.
    """
    stamp = fetched_at or datetime.now(UTC)
    indicators = series_count = points = inserted = unchanged = skipped = 0
    for indicator in fetch.parse.indicators:
        if indicator.latest_value is None or indicator.period is None:
            skipped += 1
            continue
        indicators += 1
        try:
            series = ensure_series(session, dataset, {"INDICATOR": indicator.code})
        except SeriesDefinitionError:
            skipped += 1
            continue
        series_count += 1
        point_list = [point for point in indicator.points if point[0] >= start]
        points += len(point_list)
        result = record_observations(
            session,
            series.id,
            point_list,
            fetched_at=stamp,
            raw_object_key=fetch.raw_object_key,
        )
        inserted += result.inserted
        unchanged += result.unchanged
        periods = [period for period, _ in point_list]
        if periods:
            first, last = min(periods), max(periods)
            if series.coverage_start is None or first < series.coverage_start:
                series.coverage_start = first
            if series.coverage_end is None or last > series.coverage_end:
                series.coverage_end = last
    session.flush()
    return SectorIngest(
        indicators=indicators,
        series=series_count,
        points=points,
        inserted=inserted,
        unchanged=unchanged,
        skipped=skipped,
    )


__all__ = [
    "CHANNEL",
    "DEFAULT_BASE_URL",
    "INSTITUTION",
    "SectorFetch",
    "SectorIngest",
    "TurcatClient",
    "TurcatConnector",
    "TurcatResponse",
    "ingest_sector",
    "parse_turcat_response",
]
