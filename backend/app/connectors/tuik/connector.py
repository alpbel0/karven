"""TÜİK databrowser2 connector (token-free JSON-stat / SDMX-CSV channel)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from app.config import Settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    ROLE_FREQUENCY,
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
    SOURCE_ERROR,
    THROTTLED,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    MinioObjectStore,
    ObjectStore,
    SeriesMeta,
    SourceConnector,
)
from app.connectors.tuik.client import DEFAULT_BASE_URL, Databrowser2Client
from app.connectors.tuik.hierarchy import resolve_parents
from app.connectors.tuik.parsers import (
    CatalogData,
    DataflowInfo,
    all_value_codes,
    jsonstat_time_dimension,
    parse_catalog,
    parse_dimension_codelist,
    parse_hidden_dimensions,
    parse_sdmx_csv_points,
    parse_structure,
    parse_time_coverage,
    points_from_jsonstat,
    selectable_codes,
    series_metas_from_sdmx_json,
)

logger = logging.getLogger(__name__)

REF_AREA_DIMENSION = "REF_AREA"
DEFAULT_REF_AREA = "TR"

# A single download request should not materialize more than this many cells, so
# huge dataflows are partitioned (by the dimension with the most codes) before
# fetching instead of holding millions of cells in memory at once.
MAX_CHUNK_OBSERVATIONS = 100_000
MAX_SPLIT_DEPTH = 16


@dataclass(frozen=True)
class DataflowCatalog:
    """Per-dataflow downloader result with its completeness accounting."""

    dataflow_id: str
    obs_expected: int
    obs_received: int
    series_found: int
    metas: list[SeriesMeta]

    @property
    def complete(self) -> bool:
        return self.obs_received == self.obs_expected


def ref_area_criteria(code: str) -> list[dict[str, Any]]:
    """The one filter databrowser2 needs to expand a whole dataset."""
    return [{"id": REF_AREA_DIMENSION, "filterValues": [code], "type": "CodeValues", "period": 0}]


def split_external_code(external_code: str) -> tuple[str, list[str]]:
    """Split ``<DATAFLOW_ID>:<dim1>.<dim2>...`` into its parts."""
    dataflow_id, separator, key = external_code.partition(":")
    if not separator or not dataflow_id or not key:
        raise ConnectorError(
            FORMAT_CHANGED,
            f"external_code must look like DATAFLOW_ID:dim1.dim2..., got {external_code!r}",
        )
    parts = key.split(".")
    if any(part == "" for part in parts):
        raise ConnectorError(FORMAT_CHANGED, f"empty dimension in external_code {external_code!r}")
    return dataflow_id, parts


def _role_for(code: str, label: str, dsd_ref: str | None) -> str:
    if code == "FREQ":
        return ROLE_FREQUENCY
    haystack = f"{code} {label} {dsd_ref or ''}".upper()
    if code == REF_AREA_DIMENSION or "IBBS" in haystack or "NUTS" in haystack or "AREA" in haystack:
        return ROLE_GEO
    return ROLE_OTHER


class TuikConnector(SourceConnector):
    """Lists and fetches TÜİK series through the databrowser2 API."""

    institution_code = "tuik"
    institution_name = "Türkiye İstatistik Kurumu"
    channel = "databrowser2"

    def __init__(
        self,
        *,
        client: Databrowser2Client | None = None,
        store: ObjectStore | None = None,
        http_client: Any = None,
        base_url: str = DEFAULT_BASE_URL,
        settings_obj: Settings | None = None,
    ) -> None:
        if client is None:
            if store is None:
                store = MinioObjectStore(settings_obj)
            client = Databrowser2Client(
                base_url=base_url,
                http_client=http_client,
                store=store,
                institution=self.institution_code,
                settings_obj=settings_obj,
            )
        self._client = client
        self._catalog: CatalogData | None = None
        self._catalog_raw_key: str | None = None
        self._dimension_orders: dict[str, list[str]] = {}

    @property
    def client(self) -> Databrowser2Client:
        return self._client

    @property
    def catalog_raw_object_key(self) -> str | None:
        return self._catalog_raw_key

    def close(self) -> None:
        self._client.close()

    def _ensure_catalog(self) -> CatalogData:
        if self._catalog is None:
            response = self._client.catalog()
            self._catalog_raw_key = response.raw_object_key
            self._catalog = parse_catalog(response.json())
            logger.info("tuik catalog: %d dataflows", len(self._catalog.dataflows))
        return self._catalog

    def dataflows(self) -> list[DataflowInfo]:
        """All dataflows (id + correct version) from the live catalog."""
        return list(self._ensure_catalog().dataflows.values())

    def resolve(self, dataflow: DataflowInfo | str) -> DataflowInfo:
        """Resolve a dataflow id or ``TR,ID,VERSION`` identifier to metadata."""
        if isinstance(dataflow, DataflowInfo):
            return dataflow
        catalog = self._ensure_catalog()
        direct = catalog.get(dataflow)
        if direct is not None:
            return direct
        for candidate in catalog.dataflows.values():
            if candidate.dataset_identifier == dataflow:
                return candidate
        raise ConnectorError(NOT_FOUND, f"unknown dataflow {dataflow!r}")

    # --- catalog (datasets + dimension codelists; no values) ----------------

    def list_datasets(self) -> Iterator[DatasetMeta]:
        """Yield every dataset with its dimension codelists, skipping failures."""
        for info in self.dataflows():
            try:
                yield self.dataset_meta(info)
            except ConnectorError as exc:
                logger.warning("tuik catalog: %s failed (%s): %s", info.dataflow_id, exc.kind, exc)
            except Exception:  # noqa: BLE001 - one bad dataflow must not stop the run
                logger.warning(
                    "tuik catalog: %s failed unexpectedly", info.dataflow_id, exc_info=True
                )

    def dataset_meta(self, dataflow: DataflowInfo | str) -> DatasetMeta:
        """Full dataset metadata: dimensions, code lists, defaults and coverage."""
        info = self.resolve(dataflow)
        identifier = info.dataset_identifier
        structure = self._structure(identifier, info)
        specs, time_dim = parse_structure(structure)

        obs_count: int | None = None
        coverage_start: date | None = None
        coverage_end: date | None = None
        dimensions: list[DimensionMeta] = []
        for spec in specs:
            if spec.code == time_dim:
                start, end = self._time_coverage(identifier, spec.code)
                coverage_start = start
                coverage_end = end
                dimensions.append(
                    DimensionMeta(
                        code=spec.code,
                        label=spec.label,
                        position=spec.position,
                        role=ROLE_TIME,
                        codes=[],
                        attributes=self._dimension_attributes(spec.dsd_ref, None),
                    )
                )
                continue
            response = self._client.dataset_partial_codelist(identifier, spec.code)
            try:
                data = parse_dimension_codelist(response.json(), spec.code)
            except ConnectorError as exc:
                if exc.kind != FORMAT_CHANGED:
                    raise
                # Some dataflows expose a structure but no codes at all (empty
                # since never published); catalogue the dimension with no codes.
                logger.warning(
                    "tuik catalog: %s dimension %s has no codelist",
                    info.dataflow_id,
                    spec.code,
                )
                dimensions.append(
                    DimensionMeta(
                        code=spec.code,
                        label=spec.label,
                        position=spec.position,
                        role=_role_for(spec.code, spec.label, spec.dsd_ref),
                        codes=[],
                        attributes=self._dimension_attributes(spec.dsd_ref, None),
                    )
                )
                continue
            obs_count = max(obs_count or 0, data.obs_count)
            parents, hierarchy_source = resolve_parents(
                spec.code, spec.label, spec.dsd_ref, [(e.code, e.parent_code) for e in data.entries]
            )
            codes = [
                DimensionCodeMeta(
                    code=entry.code,
                    label=entry.label,
                    parent_code=parents.get(entry.code) or None,
                    is_default=entry.is_default,
                )
                for entry in data.entries
            ]
            dimensions.append(
                DimensionMeta(
                    code=spec.code,
                    label=spec.label,
                    position=spec.position,
                    role=_role_for(spec.code, spec.label, spec.dsd_ref),
                    codes=codes,
                    attributes=self._dimension_attributes(spec.dsd_ref, hierarchy_source),
                )
            )

        attributes: dict[str, Any] = {
            "dataflow_id": info.dataflow_id,
            "agency": info.agency,
            "version": info.version,
            "channel": self.channel,
        }
        hidden = parse_hidden_dimensions(structure)
        if hidden:
            attributes["hidden_dimensions"] = hidden
        # A dimension without codes means no series of this dataset can be built;
        # flag it instead of cataloguing it as a healthy dataset.
        empty_dims = [d.code for d in dimensions if d.role != ROLE_TIME and not d.codes]
        return DatasetMeta(
            external_code=info.dataflow_id,
            name=info.title,
            description=info.description,
            source_category=info.source_category,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            obs_count=obs_count,
            attributes=attributes,
            dimensions=dimensions,
            source_incomplete=bool(empty_dims),
            source_incomplete_note=(
                f"source serves no codelist for dimensions: {', '.join(empty_dims)}"
                if empty_dims
                else None
            ),
        )

    @staticmethod
    def _dimension_attributes(dsd_ref: str | None, hierarchy_source: str | None) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        if dsd_ref:
            attributes["dsd_ref"] = dsd_ref
        if hierarchy_source:
            attributes["hierarchy_source"] = hierarchy_source
        return attributes

    def _time_coverage(self, identifier: str, time_dim: str) -> tuple[date | None, date | None]:
        try:
            response = self._client.dataset_partial_codelist(identifier, time_dim)
            return parse_time_coverage(response.json())
        except ConnectorError:
            logger.warning("tuik catalog: %s time coverage unavailable", identifier, exc_info=True)
            return None, None

    # --- completeness downloader (verification mode only) ------------------

    def catalog_dataflow(self, dataflow: DataflowInfo | str) -> DataflowCatalog:
        """Discover every series of one dataflow and prove its observation total.

        This is only used by ``catalog --verify-completeness``: it downloads
        cells (chunked) and compares the received observation total against the
        source's reported ``obsCount``.
        """
        info = self.resolve(dataflow)
        identifier = info.dataset_identifier
        structure = self._structure(identifier, info)
        dimensions, _ = self._structure_dimensions(structure)
        if not dimensions:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{info.dataflow_id}: structure exposes no selectable dimensions",
            )

        codes: dict[str, list[str]] = {}
        expected = 0
        for dimension in dimensions:
            response = self._client.dataset_partial_codelist(identifier, dimension)
            payload = response.json()
            expected = max(expected, self._obs_count(payload, info))
            values = selectable_codes(payload, dimension) or all_value_codes(payload, dimension)
            if values:
                codes[dimension] = values

        order = self._dimension_order(info)
        if not order:
            raise ConnectorError(
                FORMAT_CHANGED, f"{info.dataflow_id}: cannot determine the dimension order"
            )
        criteria = [self._criterion(dim, codes[dim]) for dim in dimensions if codes.get(dim)]
        metas, received = self._collect(info, identifier, criteria, expected, order, 0)

        return DataflowCatalog(
            dataflow_id=info.dataflow_id,
            obs_expected=expected,
            obs_received=received,
            series_found=len(metas),
            metas=metas,
        )

    def _structure(self, dataset_identifier: str, info: DataflowInfo) -> dict[str, Any]:
        response = self._client.dataset_structure(dataset_identifier)
        payload = response.json()
        if isinstance(payload, dict) and payload.get("criteria"):
            return payload
        code = payload.get("errorCode") if isinstance(payload, dict) else None
        kind = NOT_FOUND if code == "DATAFLOW_NOT_FOUND" else SOURCE_ERROR
        raise ConnectorError(
            kind,
            f"{info.dataflow_id}: structure unavailable ({code or payload})",
            raw_object_key=response.raw_object_key,
        )

    @staticmethod
    def _structure_dimensions(structure: dict[str, Any]) -> tuple[list[str], str]:
        time_dim = str(structure.get("timeDimension") or "TIME_PERIOD")
        ids: list[str] = []
        for criterion in structure.get("criteria") or []:
            if isinstance(criterion, dict) and criterion.get("id"):
                ids.append(str(criterion["id"]))
        hidden = (structure.get("template") or {}).get("hiddenDimensions") or []
        for dimension in hidden:
            if dimension and str(dimension) not in ids:
                ids.append(str(dimension))
        return [dim for dim in ids if dim != time_dim], time_dim

    @staticmethod
    def _criterion(dimension: str, values: list[str]) -> dict[str, Any]:
        return {
            "id": dimension,
            "filterValues": list(values),
            "type": "CodeValues",
            "period": 0,
        }

    @staticmethod
    def _obs_count(payload: Any, info: DataflowInfo) -> int:
        if not isinstance(payload, dict):
            raise ConnectorError(
                FORMAT_CHANGED, f"{info.dataflow_id}: codelist response is not an object"
            )
        try:
            return int(payload.get("obsCount") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _split_index(criteria: list[dict[str, Any]]) -> int | None:
        best: int | None = None
        best_length = 1
        for index, criterion in enumerate(criteria):
            length = len(criterion.get("filterValues") or [])
            if length > best_length:
                best_length = length
                best = index
        return best

    def _expected_obs(self, dataset_identifier: str, criteria: list[dict[str, Any]]) -> int:
        if not criteria:
            return 0
        response = self._client.dataset_partial_codelist(
            dataset_identifier, str(criteria[0]["id"]), criteria
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return 0
        try:
            return int(payload.get("obsCount") or 0)
        except (TypeError, ValueError):
            return 0

    def _collect(
        self,
        info: DataflowInfo,
        dataset_identifier: str,
        criteria: list[dict[str, Any]],
        expected: int,
        order: list[str],
        depth: int,
    ) -> tuple[list[SeriesMeta], int]:
        index = self._split_index(criteria)
        if expected > MAX_CHUNK_OBSERVATIONS and depth < MAX_SPLIT_DEPTH and index is not None:
            return self._partition(
                info, dataset_identifier, criteria, index, expected, order, depth
            )
        try:
            metas, received = self._fetch_chunk(info, dataset_identifier, criteria, order)
        except ConnectorError as exc:
            if exc.kind in (NOT_FOUND, THROTTLED) or index is None or depth >= MAX_SPLIT_DEPTH:
                raise
            logger.warning(
                "tuik catalog: %s chunk failed (%s); splitting %s",
                info.dataflow_id,
                exc.kind,
                criteria[index]["id"],
            )
            return self._partition(
                info, dataset_identifier, criteria, index, expected, order, depth
            )
        if received == expected or depth >= MAX_SPLIT_DEPTH or index is None:
            return metas, received
        return self._partition(info, dataset_identifier, criteria, index, expected, order, depth)

    def _partition(
        self,
        info: DataflowInfo,
        dataset_identifier: str,
        criteria: list[dict[str, Any]],
        index: int,
        expected: int,
        order: list[str],
        depth: int,
    ) -> tuple[list[SeriesMeta], int]:
        values = list(criteria[index].get("filterValues") or [])
        if len(values) <= 1:
            return self._fetch_chunk(info, dataset_identifier, criteria, order)
        groups = max(2, -(-expected // MAX_CHUNK_OBSERVATIONS))
        groups = min(groups, len(values))
        size = -(-len(values) // groups)
        merged: dict[str, SeriesMeta] = {}
        received = 0
        for start in range(0, len(values), size):
            part = values[start : start + size]
            if not part:
                continue
            sub_criteria = list(criteria)
            sub_criteria[index] = {**criteria[index], "filterValues": part}
            sub_expected = self._expected_obs(dataset_identifier, sub_criteria)
            sub_metas, sub_received = self._collect(
                info, dataset_identifier, sub_criteria, sub_expected, order, depth + 1
            )
            self._merge_metas(merged, sub_metas)
            received += sub_received
        logger.info(
            "tuik catalog: %s split %s into %d chunks (%d obs)",
            info.dataflow_id,
            criteria[index]["id"],
            groups,
            received,
        )
        return list(merged.values()), received

    def _fetch_chunk(
        self,
        info: DataflowInfo,
        dataset_identifier: str,
        criteria: list[dict[str, Any]],
        order: list[str],
    ) -> tuple[list[SeriesMeta], int]:
        response = self._client.dataset_download_json(dataset_identifier, criteria)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{info.dataflow_id}: SDMX-JSON payload is not an object",
                raw_object_key=response.raw_object_key,
            )
        errors = payload.get("errors")
        if errors:
            raise ConnectorError(
                SOURCE_ERROR,
                f"{info.dataflow_id}: SDMX-JSON error: {str(errors)[:200]}",
                raw_object_key=response.raw_object_key,
            )
        return series_metas_from_sdmx_json(
            payload,
            dataflow_id=info.dataflow_id,
            dataflow_version=info.version,
            title=info.title,
            order=order,
            source_category=info.source_category,
            channel=self.channel,
        )

    @staticmethod
    def _merge_metas(target: dict[str, SeriesMeta], metas: list[SeriesMeta]) -> None:
        for meta in metas:
            existing = target.get(meta.external_code)
            if existing is None:
                target[meta.external_code] = meta
                continue
            count = (existing.attributes.get("value_count") or 0) + (
                meta.attributes.get("value_count") or 0
            )
            start = existing.coverage_start
            if meta.coverage_start is not None and (start is None or meta.coverage_start < start):
                start = meta.coverage_start
            end = existing.coverage_end
            if meta.coverage_end is not None and (end is None or meta.coverage_end > end):
                end = meta.coverage_end
            attributes = dict(existing.attributes)
            attributes["value_count"] = count
            target[meta.external_code] = replace(
                existing, coverage_start=start, coverage_end=end, attributes=attributes
            )

    def _ref_area_codes(self, dataset_identifier: str) -> list[str]:
        response = self._client.dataset_partial_codelist(dataset_identifier, REF_AREA_DIMENSION)
        return selectable_codes(response.json(), REF_AREA_DIMENSION)

    def _dimension_order(self, info: DataflowInfo) -> list[str]:
        cached = self._dimension_orders.get(info.dataflow_id)
        if cached is not None:
            return cached
        identifier = info.dataset_identifier
        order = self._safe_probe(identifier, ref_area_criteria(DEFAULT_REF_AREA))
        if not order:
            try:
                codes = self._ref_area_codes(identifier)
            except ConnectorError:
                codes = []
            if codes:
                order = self._safe_probe(identifier, ref_area_criteria(codes[0]))
        if not order:
            order = self._structure_order(identifier)
        self._dimension_orders[info.dataflow_id] = order
        return order

    def _safe_probe(self, dataset_identifier: str, criteria: list[dict[str, Any]]) -> list[str]:
        try:
            return self._probe_order(dataset_identifier, criteria)
        except ConnectorError:
            return []

    def _probe_order(self, dataset_identifier: str, criteria: list[dict[str, Any]]) -> list[str]:
        response = self._client.dataset_data(dataset_identifier, criteria)
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return []
        time_id = jsonstat_time_dimension(payload)
        return [str(dim_id) for dim_id in payload["id"] if dim_id != time_id]

    def _structure_order(self, dataset_identifier: str) -> list[str]:
        response = self._client.dataset_structure(dataset_identifier)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ConnectorError(
                FORMAT_CHANGED,
                f"structure response for {dataset_identifier} is not an object",
                raw_object_key=response.raw_object_key,
            )
        try:
            specs, time_dim = parse_structure(payload)
        except ConnectorError as exc:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"cannot determine the dimension order of {dataset_identifier}",
                raw_object_key=response.raw_object_key,
            ) from exc
        return [spec.code for spec in specs if spec.code != time_dim]

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        """Fetch one series; SDMX-CSV first, JSON-stat when CSV cannot be parsed.

        A filtered request that fails with an internal server error (seen live on
        some regional dataflows) is retried unfiltered and filtered locally.
        """
        info = self.resolve(dataset_code)
        identifier = info.dataset_identifier
        resolved_order = order or self._dimension_order(info)
        missing = [dim for dim in resolved_order if dim not in codes]
        if missing:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{dataset_code}: missing codes for dimensions {missing}",
            )
        key = {dim: codes[dim] for dim in resolved_order}
        criteria = [
            {"id": dim, "filterValues": [code], "type": "CodeValues", "period": 0}
            for dim, code in key.items()
        ]

        csv_key, csv_points, csv_error = self._fetch_csv(identifier, criteria, key)
        if csv_points:
            points = [point for point in csv_points if point[0] >= start]
            return FetchResult(
                external_code=_external_code(dataset_code, resolved_order, codes),
                points=points,
                raw_object_keys=[key_ for key_ in (csv_key,) if key_],
                channel=self.channel,
            )

        # Fixed, greppable text: live monitors watch for this fallback.
        logger.warning(
            "tuik csv fetch failed, falling back to json-stat: %s (%s)",
            dataset_code,
            csv_error or "no observations in CSV",
        )
        json_key, json_points, json_error = self._fetch_jsonstat(identifier, criteria, key)
        if json_points:
            points = [point for point in json_points if point[0] >= start]
            return FetchResult(
                external_code=_external_code(dataset_code, resolved_order, codes),
                points=points,
                raw_object_keys=[key_ for key_ in (json_key, csv_key) if key_],
                channel=self.channel,
            )

        if csv_error is not None or json_error is not None:
            message = "; ".join(str(error) for error in (csv_error, json_error) if error)
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{dataset_code}: could not parse source response ({message})",
                raw_object_key=json_key or csv_key,
            )
        raise ConnectorError(
            EMPTY,
            f"{dataset_code}: source returned no observations",
            raw_object_key=json_key or csv_key,
        )

    def _fetch_csv(
        self, identifier: str, criteria: list[dict[str, Any]], key: dict[str, str]
    ) -> tuple[str | None, list[tuple[date, Any]] | None, ConnectorError | None]:
        try:
            response = self._client.dataset_csv(identifier, criteria)
        except ConnectorError as exc:
            if exc.kind in (NOT_FOUND, THROTTLED):
                raise
            if exc.kind != SOURCE_ERROR:
                return None, None, exc
            try:
                fallback = self._client.dataset_csv(identifier, [])
                return fallback.raw_object_key, parse_sdmx_csv_points(fallback.content, key), None
            except ConnectorError as inner:
                return None, None, inner
        try:
            return response.raw_object_key, parse_sdmx_csv_points(response.content, key), None
        except ConnectorError as exc:
            return response.raw_object_key, None, exc

    def _fetch_jsonstat(
        self, identifier: str, criteria: list[dict[str, Any]], key: dict[str, str]
    ) -> tuple[str | None, list[tuple[date, Any]] | None, ConnectorError | None]:
        try:
            response = self._client.dataset_data(identifier, criteria)
        except ConnectorError as exc:
            if exc.kind in (NOT_FOUND, THROTTLED):
                raise
            if exc.kind != SOURCE_ERROR:
                return None, None, exc
            try:
                fallback = self._client.dataset_data(identifier, [])
                return fallback.raw_object_key, points_from_jsonstat(fallback.json(), key), None
            except ConnectorError as inner:
                return None, None, inner
        try:
            return response.raw_object_key, points_from_jsonstat(response.json(), key), None
        except ConnectorError as exc:
            return response.raw_object_key, None, exc


def _external_code(dataset_code: str, order: list[str], codes: dict[str, str]) -> str:
    return f"{dataset_code}:" + ".".join(codes[dim] for dim in order)
