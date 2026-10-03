"""TCMB EVDS3 connector.

The catalog lists category/datagroup pairs for several agencies on one channel.
The connector serves one of them per instance (see :data:`SOURCES`): ``tcmb``
keeps groups whose ``DATASOURCE_ENG`` is exactly ``CBRT`` (243 live), ``hmb``
keeps the Ministry of Treasury and Finance groups (24 live), and ``tuik-evds``
keeps one TÜİK-owned statistics group (``bie_gsyhuretcar``, datagroup code
allow-listed). Each such group
becomes one dataset whose single ``SERIE`` dimension carries one code per series
(frequency, unit and aggregation live in the code's attributes, as on the Turcat
datasets). Every series is fetched at its own native frequency with its own
default aggregation; nothing here converts frequencies or invents periods.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.config import Settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    ROLE_OTHER,
    SOURCE_ERROR,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    MinioObjectStore,
    ObjectStore,
    SourceConnector,
)
from app.connectors.tcmb.client import DEFAULT_BASE_URL, EvdsClient
from app.connectors.tcmb.parsers import (
    CatalogGroup,
    SerieInfo,
    frequency_code_for_label,
    parse_bounds,
    parse_catalog,
    parse_data,
    parse_serie_list,
)

logger = logging.getLogger(__name__)

INSTITUTION = "tcmb"
INSTITUTION_NAME = "Türkiye Cumhuriyet Merkez Bankası"
CHANNEL = "evds3"
SERIE_DIMENSION = "SERIE"


@dataclass(frozen=True)
class SourceSpec:
    """One institution publishing groups on the shared EVDS3 channel.

    ``groups`` is an optional allow-list of ``DATAGROUP_CODE`` values: when set,
    only those groups are ever listed or fetched. ``None`` keeps the current
    behaviour (every group matching ``data_source``), used by ``tcmb``/``hmb``.
    """

    institution_code: str
    institution_name: str
    data_source: str
    groups: frozenset[str] | None = None


# The same EVDS3 channel publishes the central bank's, the ministry's and the
# statistics agency's datagroups; ``data_source`` is the exact ``DATASOURCE_ENG``
# they are filtered by, and ``groups`` narrows a source to specific datagroups.
SOURCES: dict[str, SourceSpec] = {
    "tcmb": SourceSpec(
        institution_code=INSTITUTION,
        institution_name=INSTITUTION_NAME,
        data_source="CBRT",
    ),
    "hmb": SourceSpec(
        institution_code="hmb",
        institution_name="T.C. Hazine ve Maliye Bakanlığı",
        data_source="Ministry of Treasury and Finance",
    ),
    "tuik-evds": SourceSpec(
        institution_code="tuik",
        institution_name="Türkiye İstatistik Kurumu",
        data_source="TURKSTAT",
        groups=frozenset({"bie_gsyhuretcar"}),
    ),
}


def _format_evds_date(value: date) -> str:
    return f"{value.day:02d}-{value.month:02d}-{value.year:04d}"


def _category_path_attribute(group: CatalogGroup) -> list[dict[str, Any]]:
    """The frozen path entries as plain JSON-ready dicts (empty EN omitted)."""
    path: list[dict[str, Any]] = []
    for entry in group.category_path:
        item: dict[str, Any] = {"id": entry.id, "level": entry.level, "title": entry.title}
        if entry.title_en:
            item["title_en"] = entry.title_en
        path.append(item)
    return path


def _dataset_attributes(group: CatalogGroup) -> dict[str, Any]:
    """Group attributes with empty values omitted (spec: omit keys whose value is empty)."""
    attributes: dict[str, Any] = {}
    for key, value in (
        ("name_en", group.name_en),
        ("category_id", group.category_id),
        ("category_title_en", group.category_title_en),
        ("source_frequency", group.source_frequency),
        ("data_source_en", group.data_source_en),
        ("unit", group.unit),
        ("unit_en", group.unit_en),
        ("source_last_updated", group.source_last_updated),
    ):
        if value:
            attributes[key] = value
    if group.category_path:
        attributes["category_path"] = _category_path_attribute(group)
    attributes["channel"] = CHANNEL
    return attributes


def _code_attributes(serie: SerieInfo, group: CatalogGroup) -> dict[str, Any]:
    attributes: dict[str, Any] = {"frequency": serie.frequency}
    if group.unit:
        attributes["unit"] = group.unit
    attributes["aggregation"] = serie.aggregation
    attributes["aggregations"] = list(serie.aggregations)
    attributes["source_frequency"] = serie.source_frequency
    attributes["name_en"] = serie.name_en
    attributes["level"] = serie.level
    return attributes


class TcmbConnector(SourceConnector):
    """Lists and fetches one institution's EVDS3 datagroups (``tcmb`` or ``hmb``)."""

    institution_code = INSTITUTION
    institution_name = INSTITUTION_NAME
    channel = CHANNEL

    def __init__(
        self,
        *,
        source: str = "tcmb",
        client: EvdsClient | None = None,
        store: ObjectStore | None = None,
        http_client: httpx.Client | None = None,
        base_url: str | None = None,
        settings_obj: Settings | None = None,
        limit: int | None = None,
        only_group: str | None = None,
    ) -> None:
        spec = SOURCES.get(source)
        if spec is None:
            raise ValueError(f"unknown TCMB source {source!r}")
        self.source = source
        self.institution_code = spec.institution_code
        self.institution_name = spec.institution_name
        self._data_source = spec.data_source
        self._allowed_groups = spec.groups
        if client is None:
            if store is None:
                store = MinioObjectStore(settings_obj)
            client = EvdsClient(
                base_url=base_url,
                http_client=http_client,
                store=store,
                institution=self.institution_code,
                settings_obj=settings_obj,
            )
        self._client = client
        self._limit = limit
        self._only_group = only_group
        self._cache: dict[str, list[SerieInfo]] = {}
        # Groups whose series list failed during ``list_datasets``: never skipped
        # silently, the CLI reports them and exits non-zero.
        self.failed_groups: list[tuple[str, str, str]] = []

    @property
    def client(self) -> EvdsClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def _serie_list(self, group_code: str) -> list[SerieInfo]:
        """Fetch (and cache) one group's series list; an unknown group is NOT_FOUND.

        A group outside this source's allow-list is rejected before any request:
        ``--group``/``fetch`` on another agency's datagroup must never silently
        reach it.
        """
        if not self._is_allowed_group(group_code):
            raise ConnectorError(
                NOT_FOUND,
                f"datagroup {group_code!r} is not in source {self.source!r}",
            )
        if group_code in self._cache:
            return self._cache[group_code]
        try:
            response = self._client.get_serie_list(group_code)
        except ConnectorError as exc:
            # The source answers an unknown datagroup with an HTML 500; the group
            # list is the only way to tell, so report it as not_found.
            if exc.kind == SOURCE_ERROR:
                raise ConnectorError(NOT_FOUND, f"unknown TCMB datagroup {group_code!r}") from exc
            raise
        series = parse_serie_list(response.json(), group_code)
        self._cache[group_code] = series
        return series

    # --- catalog -----------------------------------------------------------

    def _is_allowed_group(self, code: str) -> bool:
        """Whether ``code`` is inside this source's allow-list (``None`` = all)."""
        return self._allowed_groups is None or code in self._allowed_groups

    def list_datasets(self) -> Iterator[DatasetMeta]:
        catalog = parse_catalog(self._client.get_catalog().json(), data_source=self._data_source)
        selected = [
            group
            for group in catalog
            if self._is_allowed_group(group.code)
            and (self._only_group is None or group.code == self._only_group)
        ]
        if self._limit is not None:
            selected = selected[: self._limit]
        for group in selected:
            try:
                series = self._serie_list(group.code)
            except ConnectorError as exc:
                logger.warning("tcmb catalog: %s failed (%s): %s", group.code, exc.kind, exc)
                self.failed_groups.append((group.code, exc.kind, str(exc)))
                continue
            yield self._dataset_meta(group, series)

    def _dataset_meta(self, group: CatalogGroup, series: list[SerieInfo]) -> DatasetMeta:
        attributes = _dataset_attributes(group)
        if not series:
            attributes["no_series"] = True
        codes = [
            DimensionCodeMeta(
                code=serie.code,
                label=serie.name,
                parent_code=serie.parent_code,
                attributes=_code_attributes(serie, group),
            )
            for serie in series
        ]
        dimension = DimensionMeta(
            code=SERIE_DIMENSION,
            label="Seri",
            position=0,
            role=ROLE_OTHER,
            codes=codes,
        )
        return DatasetMeta(
            external_code=group.code,
            name=group.name,
            source_category=group.category_title or None,
            attributes=attributes,
            dimensions=[dimension],
        )

    # --- values ------------------------------------------------------------

    @staticmethod
    def _data_body(
        serie: SerieInfo,
        serie_code: str,
        frequency_code: str,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        return {
            "type": "json",
            "series": serie_code,
            "aggregationTypes": serie.aggregation,
            "formulas": "0",
            "startDate": _format_evds_date(start),
            "endDate": _format_evds_date(end),
            "frequency": frequency_code,
            "decimalSeperator": ".",
            "decimal": "10",
            "dateFormat": "0",
            "lang": "tr",
            "yon": "0",
            "sira": "0",
            "ozelFormuller": [],
            "groupSeperator": False,
            "isRaporSayfasi": False,
        }

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        if set(codes) != {SERIE_DIMENSION}:
            raise ConnectorError(
                NOT_FOUND,
                f"{dataset_code}: expected exactly one SERIE code, got {sorted(codes)}",
            )
        serie_code = codes[SERIE_DIMENSION]
        if not serie_code:
            raise ConnectorError(NOT_FOUND, f"{dataset_code}: empty SERIE code")
        serie = next(
            (item for item in self._serie_list(dataset_code) if item.code == serie_code),
            None,
        )
        if serie is None:
            raise ConnectorError(NOT_FOUND, f"{dataset_code}: no series with code {serie_code!r}")

        bounds = parse_bounds(self._client.get_bounds(serie_code, group=dataset_code).json())
        expected_code = frequency_code_for_label(serie.source_frequency)
        if bounds.frequency_code != expected_code:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{serie_code}: bounds frequency {bounds.frequency_code!r} does not match "
                f"source frequency {serie.source_frequency!r}",
            )
        if bounds.max_start_date is None or bounds.min_end_date is None:
            raise ConnectorError(EMPTY, f"{serie_code}: no coverage window")
        effective_start = max(start, bounds.max_start_date)
        end = bounds.min_end_date
        if effective_start > end:
            raise ConnectorError(
                EMPTY,
                f"{serie_code}: empty window {effective_start.isoformat()}..{end.isoformat()}",
            )

        body = self._data_body(serie, serie_code, bounds.frequency_code, effective_start, end)
        response = self._client.get_data(body, group=dataset_code)
        points = parse_data(response.json(), serie_code, bounds.frequency_code)
        if not points:
            raise ConnectorError(EMPTY, f"{serie_code}: no observations")
        return FetchResult(
            external_code=serie_code,
            points=points,
            raw_object_keys=[response.raw_object_key] if response.raw_object_key else [],
            channel=self.channel,
            source_updated_at=None,
        )


__all__ = [
    "CHANNEL",
    "DEFAULT_BASE_URL",
    "INSTITUTION",
    "INSTITUTION_NAME",
    "SERIE_DIMENSION",
    "SOURCES",
    "SourceSpec",
    "TcmbConnector",
]
