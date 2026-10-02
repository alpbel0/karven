"""TÜİK databrowser2 connector (token-free JSON-stat / SDMX-CSV channel)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
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
from app.connectors.tuik.nsiws import FALLBACK_KINDS, NsiwsClient
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
        nsiws: NsiwsClient | None = None,
        known_dataflows: Iterable[DataflowInfo] = (),
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
        self._nsiws = nsiws
        self._known = {info.dataflow_id: replace(info, listed=False) for info in known_dataflows}
        self._catalog: CatalogData | None = None
        self._catalog_raw_key: str | None = None
        self._dimension_orders: dict[str, list[str]] = {}
        self._dimension_order_verified: dict[str, bool] = {}
        self._dimension_labels: dict[str, dict[str, str]] = {}

    @property
    def client(self) -> Databrowser2Client:
        return self._client

    @property
    def catalog_raw_object_key(self) -> str | None:
        return self._catalog_raw_key

    def close(self) -> None:
        self._client.close()
        if self._nsiws is not None:
            self._nsiws.close()

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

    def unlisted_dataflows(self) -> list[DataflowInfo]:
        """Known dataflows (from the database) that the live listing no longer has."""
        catalog = self._ensure_catalog()
        return [info for info in self._known.values() if catalog.get(info.dataflow_id) is None]

    def resolve(self, dataflow: DataflowInfo | str) -> DataflowInfo:
        """Resolve a dataflow id or ``TR,ID,VERSION`` identifier to metadata.

        The live listing wins; known dataflows that dropped out of it are the
        fallback, so they stay refreshable and fetchable by their id.
        """
        if isinstance(dataflow, DataflowInfo):
            return dataflow
        catalog = self._ensure_catalog()
        direct = catalog.get(dataflow)
        if direct is not None:
            return direct
        for candidate in catalog.dataflows.values():
            if candidate.dataset_identifier == dataflow:
                return candidate
        known = self._known.get(dataflow)
        if known is not None:
            return known
        for candidate in self._known.values():
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
        """Full dataset metadata: dimensions, code lists, defaults and coverage.

        The catalogued non-time dimensions are exactly the dataflow's *data
        dimensions* — the JSON-stat ``id`` order from a live probe — because many
        dataflows carry hidden dimensions (structure ``hiddenDimensions``) whose
        codes really are part of the series identity. Hidden dimensions absent
        from the data stay in ``attributes['hidden_dimensions']`` as pure view
        settings; catalogued dimensions that did come from ``hiddenDimensions``
        carry ``attributes['from_hidden'] = true``.

        When the data dimensions cannot be probed, the structure criteria are
        catalogued instead, the dataset is marked ``source_incomplete`` and a
        WARNING is logged; hidden dimensions are never guessed as data.
        """
        info = self.resolve(dataflow)
        identifier = info.dataset_identifier
        structure = self._structure(identifier, info)
        specs, time_dim = parse_structure(structure)
        spec_by_code = {spec.code: spec for spec in specs}
        structure_dims = [spec.code for spec in specs if spec.code != time_dim]
        hidden = parse_hidden_dimensions(structure)

        data_dims, verified = self._data_dimension_order(info, fallback=structure_dims)
        if not verified:
            logger.warning(
                "tuik catalog: %s data dimensions could not be verified; "
                "cataloguing structure criteria only",
                info.dataflow_id,
            )
        data_dim_set = set(data_dims)
        hidden_only = [dim for dim in hidden if dim not in data_dim_set]
        probe_labels = self._dimension_labels.get(info.dataflow_id, {})

        obs_count: int | None = None
        dimensions: list[DimensionMeta] = []
        for position, code in enumerate(data_dims):
            spec = spec_by_code.get(code)
            dsd_ref = spec.dsd_ref if spec is not None else None
            label = spec.label if spec is not None else probe_labels.get(code) or ""
            response = self._client.dataset_partial_codelist(identifier, code)
            try:
                data = parse_dimension_codelist(response.json(), code)
            except ConnectorError as exc:
                if exc.kind != FORMAT_CHANGED:
                    raise
                # Some dataflows expose a structure but no codes at all (empty
                # since never published); catalogue the dimension with no codes.
                logger.warning(
                    "tuik catalog: %s dimension %s has no codelist",
                    info.dataflow_id,
                    code,
                )
                dimensions.append(
                    DimensionMeta(
                        code=code,
                        label=label or code,
                        position=position,
                        role=_role_for(code, label or code, dsd_ref),
                        codes=[],
                        attributes=self._dimension_attributes(
                            dsd_ref, None, from_hidden=code in hidden
                        ),
                    )
                )
                continue
            obs_count = max(obs_count or 0, data.obs_count)
            if not label:
                label = data.dimension_label or code
            parents, hierarchy_source = resolve_parents(
                code, label, dsd_ref, [(e.code, e.parent_code) for e in data.entries]
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
                    code=code,
                    label=label,
                    position=position,
                    role=_role_for(code, label, dsd_ref),
                    codes=codes,
                    attributes=self._dimension_attributes(
                        dsd_ref, hierarchy_source, from_hidden=code in hidden
                    ),
                )
            )

        time_spec = spec_by_code.get(time_dim)
        coverage_start, coverage_end = self._time_coverage(identifier, time_dim)
        dimensions.append(
            DimensionMeta(
                code=time_dim,
                label=time_spec.label if time_spec is not None else "Time period",
                position=len(data_dims),
                role=ROLE_TIME,
                codes=[],
                attributes=self._dimension_attributes(
                    time_spec.dsd_ref if time_spec is not None else None, None
                ),
            )
        )

        attributes: dict[str, Any] = {
            "dataflow_id": info.dataflow_id,
            "agency": info.agency,
            "version": info.version,
            "channel": self.channel,
            "data_dimensions_verified": verified,
        }
        if hidden_only:
            attributes["hidden_dimensions"] = hidden_only
        # A dimension without codes means no series of this dataset can be built;
        # flag it instead of cataloguing it as a healthy dataset.
        empty_dims = [d.code for d in dimensions if d.role != ROLE_TIME and not d.codes]
        notes: list[str] = []
        if empty_dims:
            notes.append(f"source serves no codelist for dimensions: {', '.join(empty_dims)}")
        if not verified:
            notes.append(
                "data dimensions could not be verified from the source data; "
                "catalogued structure criteria only"
            )
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
            source_incomplete=bool(notes),
            source_incomplete_note="; ".join(notes) if notes else None,
        )

    @staticmethod
    def _dimension_attributes(
        dsd_ref: str | None, hierarchy_source: str | None, *, from_hidden: bool = False
    ) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        if dsd_ref:
            attributes["dsd_ref"] = dsd_ref
        if hierarchy_source:
            attributes["hierarchy_source"] = hierarchy_source
        if from_hidden:
            attributes["from_hidden"] = True
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
        structure_dims, _ = self._structure_dimensions(structure)
        criteria_dims = self._criteria_dimensions(structure)
        data_dims, verified = self._data_dimension_order(info, fallback=criteria_dims)
        if not structure_dims and not data_dims:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{info.dataflow_id}: structure exposes no selectable dimensions",
            )
        if verified:
            # The data's own dimension list is the complete set to enumerate.
            dimensions = data_dims
        else:
            # No verified data dimensions: enumerate everything the structure
            # exposes (including hidden dimensions) for completeness, but keep
            # the series identity to the structure criteria so a hidden view
            # default is never guessed as part of the series key.
            dimensions = structure_dims
            logger.warning(
                "tuik catalog: %s data dimensions could not be verified; "
                "completeness uses structure dimensions",
                info.dataflow_id,
            )
        order = data_dims

        codes: dict[str, list[str]] = {}
        expected = 0
        for dimension in dimensions:
            response = self._client.dataset_partial_codelist(identifier, dimension)
            payload = response.json()
            expected = max(expected, self._obs_count(payload, info))
            values = selectable_codes(payload, dimension) or all_value_codes(payload, dimension)
            if values:
                codes[dimension] = values

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
        """Structure-declared non-time dimensions (criteria plus hidden).

        Used only as a fallback when the live data probe cannot verify the data
        dimensions; hidden dimensions are included here because they may still be
        filterable even when their series role is unknown.
        """
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
    def _criteria_dimensions(structure: dict[str, Any]) -> list[str]:
        """Structure criteria non-time dimension ids, in declared order (no hidden)."""
        time_dim = str(structure.get("timeDimension") or "TIME_PERIOD")
        ids: list[str] = []
        for criterion in structure.get("criteria") or []:
            if isinstance(criterion, dict) and criterion.get("id"):
                code = str(criterion["id"])
                if code != time_dim and code not in ids:
                    ids.append(code)
        return ids

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

    def _data_dimension_order(
        self, info: DataflowInfo, *, fallback: list[str] | None = None
    ) -> tuple[list[str], bool]:
        """Return ``(data_dimensions, verified)`` for one dataflow (cached).

        ``data_dimensions`` is the JSON-stat ``id`` order from a live data probe,
        i.e. exactly the dimensions the source actually serves. ``verified`` is
        True only when that probe succeeded; otherwise ``fallback`` (or the
        structure criteria) is used and the data dimensions are unverified, so a
        caller can refuse to guess hidden view defaults as series dimensions.
        """
        cached = self._dimension_orders.get(info.dataflow_id)
        if cached is not None:
            return cached, self._dimension_order_verified.get(info.dataflow_id, False)
        identifier = info.dataset_identifier
        order: list[str] = []
        labels: dict[str, str] = {}
        probed = self._probe_dimensions(identifier, ref_area_criteria(DEFAULT_REF_AREA))
        if probed is not None:
            order, labels = probed
        if not order:
            try:
                codes = self._ref_area_codes(identifier)
            except ConnectorError:
                codes = []
            if codes:
                probed = self._probe_dimensions(identifier, ref_area_criteria(codes[0]))
                if probed is not None:
                    order, labels = probed
        verified = bool(order)
        if not order:
            order = list(fallback) if fallback is not None else self._structure_order(identifier)
        self._dimension_orders[info.dataflow_id] = order
        self._dimension_order_verified[info.dataflow_id] = verified
        self._dimension_labels[info.dataflow_id] = labels
        return order, verified

    def _dimension_order(self, info: DataflowInfo) -> list[str]:
        return self._data_dimension_order(info)[0]

    def _probe_dimensions(
        self, dataset_identifier: str, criteria: list[dict[str, Any]]
    ) -> tuple[list[str], dict[str, str]] | None:
        """Probe JSON-stat for ``(dimension order, dimension labels)`` or None."""
        try:
            response = self._client.dataset_data(dataset_identifier, criteria)
            payload = response.json()
        except ConnectorError:
            return None
        if not isinstance(payload, dict) or not payload.get("id"):
            return None
        time_id = jsonstat_time_dimension(payload)
        order = [str(dim_id) for dim_id in payload["id"] if dim_id != time_id]
        labels: dict[str, str] = {}
        for dim_id, dim_info in (payload.get("dimension") or {}).items():
            if isinstance(dim_info, dict) and dim_info.get("label"):
                labels[str(dim_id)] = str(dim_info["label"])
        return order, labels

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
        """Fetch one series from databrowser2, backing off to nsiws on failure.

        Failure kinds that trigger the official nsiws channel: ``timeout``,
        ``source_error``, ``throttled`` or ``not_found``. A parse/format error
        (``format_changed``) or an empty result is not retried on nsiws.
        """
        try:
            return self._fetch_databrowser2(dataset_code, codes, order=order, start=start)
        except ConnectorError as exc:
            if self._nsiws is None or exc.kind not in FALLBACK_KINDS:
                raise
            # Fixed, greppable text: live monitors watch for this fallback.
            logger.warning(
                "tuik databrowser2 failed (%s), falling back to nsiws: %s",
                exc.kind,
                dataset_code,
            )
            version = self.resolve(dataset_code).version
            return self._nsiws.fetch_series(
                dataset_code, codes, version=version, order=order, start=start
            )

    def _fetch_databrowser2(
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
