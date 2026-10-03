"""Source-agnostic connector interface and shared catalog/ingest helpers.

A connector wraps one source channel (TÜİK databrowser2 is the first) behind a
small protocol: enumerate datasets with their complete dimension code lists,
fetch one series built from a dataset plus one code per dimension, and report
failures with a machine-readable ``kind`` so the later fetching agent can
diagnose.

This module owns the write helpers connectors need:

- :func:`sync_catalog` upserts the institution, its datasets, dimensions and
  dimension codes (no observation values, no pre-created series).
- :func:`ensure_series` creates a series row the first time a breakdown is used.
- :func:`ingest_series` fetches one series and appends observations through
  ``record_observations`` (never writing values itself).

Raw responses are stored in MinIO under
``sources/<institution>/YYYY/MM/DD/<dataset>/<channel>-<uuid>.<ext>`` so every
observation can point back at the exact payload it came from.
"""

from __future__ import annotations

import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.data.errors import SeriesDefinitionError
from app.data.models import Dataset, DatasetDimension, DimensionCode, Institution, Series
from app.data.observations import RecordResult, record_observations
from app.data.periods import FREQUENCIES, frequency_for_sdmx_code

logger = logging.getLogger(__name__)

# ConnectorError kinds. The data-fetching agent routes on these.
NOT_FOUND = "not_found"
EMPTY = "empty"
TIMEOUT = "timeout"
FORMAT_CHANGED = "format_changed"
SOURCE_ERROR = "source_error"
THROTTLED = "throttled"
#: The series is catalogued, but no on-demand connector is wired for its channel.
NO_CONNECTOR = "no_connector"

ERROR_KINDS = (NOT_FOUND, EMPTY, TIMEOUT, FORMAT_CHANGED, SOURCE_ERROR, THROTTLED, NO_CONNECTOR)

# Catalog-sync outcomes.
INSERTED = "inserted"
UPDATED = "updated"
UNCHANGED = "unchanged"

# Attribute marking a dimension code that only a generated report exposed.
DISCOVERED_FROM_REPORT = "discovered_from_report"

# Dimension roles (frozen by the schema check constraint).
ROLE_TIME = "time"
ROLE_GEO = "geo"
ROLE_FREQUENCY = "frequency"
ROLE_OTHER = "other"
DIMENSION_ROLES = (ROLE_TIME, ROLE_GEO, ROLE_FREQUENCY, ROLE_OTHER)


class ConnectorError(Exception):
    """A connector failure with a stable ``kind`` and optional offending raw key."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        raw_object_key: str | None = None,
    ) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown connector error kind {kind!r}")
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.raw_object_key = raw_object_key


@dataclass(frozen=True)
class SeriesMeta:
    """Legacy series metadata (used by the SDMX-JSON downloader/verification)."""

    external_code: str
    name: str
    frequency: str
    unit: str | None = None
    source_category: str | None = None
    breakdown: dict[str, Any] = field(default_factory=dict)
    coverage_start: date | None = None
    coverage_end: date | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DimensionCodeMeta:
    """One code of a dataset dimension."""

    code: str
    label: str
    parent_code: str | None = None
    is_default: bool = False
    # Optional per-code metadata. Sources without a FREQ/UNIT_MEASURE dimension
    # (e.g. Turcat) put the frequency and unit here instead; the series builder
    # reads them as a fallback.
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DimensionMeta:
    """One dataset dimension and its complete code list."""

    code: str
    label: str
    position: int
    role: str
    codes: list[DimensionCodeMeta] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetMeta:
    """A source dataset with every dimension's codelist; no observation values."""

    external_code: str
    name: str
    description: str | None = None
    source_category: str | None = None
    coverage_start: date | None = None
    coverage_end: date | None = None
    obs_count: int | None = None
    source_incomplete: bool = False
    source_incomplete_note: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    dimensions: list[DimensionMeta] = field(default_factory=list)


@dataclass(frozen=True)
class FetchResult:
    """One successful series fetch."""

    external_code: str
    points: list[tuple[date, Decimal | None]]
    raw_object_keys: list[str]
    channel: str
    source_updated_at: datetime | None = None


class SourceConnector(ABC):
    """Common interface every source connector implements."""

    institution_code: str = ""
    institution_name: str = ""
    channel: str = ""

    @abstractmethod
    def list_datasets(self) -> Iterator[DatasetMeta]:
        """Yield the datasets this source currently exposes (with codelists)."""

    @abstractmethod
    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        """Fetch one series (dataset + one code per non-time dimension)."""

    def register_discovered_codes(
        self, session: Session, dataset: Dataset, codes: dict[str, str]
    ) -> None:
        """Persist codes that only a fetched report exposes (default no-op).

        Connectors whose catalog walk cannot list every dimension code (e.g. the
        TÜİK tourism income/expenditure reports) override this so an on-demand
        ``fetch`` never hits ``SeriesDefinitionError`` before the series row is
        created.
        """
        return None


class ObjectStore(Protocol):
    """Minimal object-store interface used for raw payloads."""

    def put(
        self, key: str, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Store ``payload`` at ``key``, raising on failure."""


class InMemoryObjectStore:
    """Test double that keeps every stored object in a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(
        self, key: str, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.objects[key] = payload


class MinioObjectStore:
    """MinIO/S3-backed store implemented with boto3 (synchronous)."""

    def __init__(self, settings_obj: Any = None) -> None:
        import boto3

        resolved = settings_obj or settings
        bucket = resolved.minio_bucket_raw
        if not bucket:
            raise ValueError("MINIO_BUCKET_RAW must be configured for raw storage")
        secret = resolved.minio_secret_key
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=resolved.minio_endpoint,
            aws_access_key_id=resolved.minio_access_key,
            aws_secret_access_key=secret.get_secret_value() if secret else None,
            region_name="us-east-1",
        )

    def put(
        self, key: str, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=payload, ContentType=content_type
        )


_EXT_CONTENT_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "html": "text/html",
    "xml": "application/xml",
}

_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(value: str) -> str:
    return _UNSAFE_SEGMENT.sub("_", value).lstrip(".").strip("_") or "unknown"


def store_raw(
    institution: str,
    dataset: str,
    channel: str,
    payload: bytes,
    ext: str,
    *,
    store: ObjectStore | None = None,
    now: datetime | None = None,
) -> str:
    """Store one raw payload and return its object key.

    Key shape: ``sources/<institution>/YYYY/MM/DD/<dataset>/<channel>-<uuid>.<ext>``.
    """
    timestamp = now or datetime.now(UTC)
    key = (
        f"sources/{_safe_segment(institution)}/{timestamp:%Y/%m/%d}/"
        f"{_safe_segment(dataset)}/{_safe_segment(channel)}-{uuid.uuid4().hex}.{_safe_segment(ext)}"
    )
    content_type = _EXT_CONTENT_TYPES.get(ext, "application/octet-stream")
    (store if store is not None else MinioObjectStore()).put(
        key, payload, content_type=content_type
    )
    return key


@dataclass(frozen=True)
class CatalogSyncResult:
    """How many datasets rows a :func:`sync_catalog` call inserted/updated/left."""

    institution_id: int
    inserted: int
    updated: int
    unchanged: int
    dimensions: int
    codes: int


def upsert_institution(session: Session, code: str, name: str) -> Institution:
    """Return the institution row for ``code``, creating it if needed."""
    institution = session.scalar(sa.select(Institution).where(Institution.code == code))
    if institution is None:
        institution = Institution(code=code, name=name)
        session.add(institution)
        session.flush()
        return institution
    if name and institution.name != name:
        institution.name = name
    session.flush()
    return institution


_DATASET_FIELDS = ("name", "description", "source_category", "obs_count", "attributes")

# ``attributes`` keys owned by another channel. ``upsert_dataset`` overwrites the
# whole attributes object, so a later run of a different channel (e.g. the
# databrowser2 catalog) would wipe a key another channel wrote (today the
# veriportali dataflow catalog merges into the same TÜİK datasets). A key here is
# preserved when the incoming ``meta.attributes`` does not itself carry it.
CHANNEL_OWNED_ATTRIBUTES = ("veriportali",)


def _preserve_channel_attributes(dataset: Dataset, incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge in ``attributes`` keys owned by another channel, if the dataset has them."""
    merged = dict(incoming)
    existing = dataset.attributes or {}
    for key in CHANNEL_OWNED_ATTRIBUTES:
        if key not in merged and key in existing:
            merged[key] = existing[key]
    return merged


def upsert_dataset(session: Session, institution_id: int, meta: DatasetMeta) -> tuple[str, Dataset]:
    """Upsert one dataset with its dimensions and code lists.

    Codes that disappear from the source are kept but marked with
    ``attributes['removed_at']``; they are never deleted. Coverage only widens.

    ``attributes`` keys owned by another channel (:data:`CHANNEL_OWNED_ATTRIBUTES`)
    survive when the incoming metadata does not carry them, so a databrowser2
    catalog run never wipes the veriportali dataflow attributes.
    """
    dataset = session.scalar(
        sa.select(Dataset).where(
            Dataset.institution_id == institution_id,
            Dataset.external_code == meta.external_code,
        )
    )
    created = dataset is None
    if dataset is None:
        dataset = Dataset(
            institution_id=institution_id,
            external_code=meta.external_code,
            name=meta.name,
        )
        session.add(dataset)
        session.flush()
    changed = created
    for field_name in _DATASET_FIELDS:
        value = getattr(meta, field_name)
        if field_name == "attributes":
            value = _preserve_channel_attributes(dataset, value)
        if getattr(dataset, field_name) != value:
            setattr(dataset, field_name, value)
            changed = True
    if meta.coverage_start is not None and (
        dataset.coverage_start is None or meta.coverage_start < dataset.coverage_start
    ):
        dataset.coverage_start = meta.coverage_start
        changed = True
    if meta.coverage_end is not None and (
        dataset.coverage_end is None or meta.coverage_end > dataset.coverage_end
    ):
        dataset.coverage_end = meta.coverage_end
        changed = True
    if meta.source_incomplete and (
        not dataset.source_incomplete
        or dataset.source_incomplete_note != meta.source_incomplete_note
    ):
        dataset.source_incomplete = True
        dataset.source_incomplete_note = meta.source_incomplete_note
        changed = True

    dimensions = 0
    codes = 0
    existing_dims = {dimension.code: dimension for dimension in dataset.dimensions}
    for dim_meta in meta.dimensions:
        dimension = existing_dims.get(dim_meta.code)
        if dimension is None:
            dimension = DatasetDimension(
                dataset_id=dataset.id,
                code=dim_meta.code,
                label=dim_meta.label,
                position=dim_meta.position,
                role=dim_meta.role,
                attributes=dict(dim_meta.attributes),
            )
            session.add(dimension)
            session.flush()
            changed = True
        else:
            if (
                dimension.label != dim_meta.label
                or dimension.position != dim_meta.position
                or dimension.role != dim_meta.role
                or dimension.attributes != dim_meta.attributes
            ):
                dimension.label = dim_meta.label
                dimension.position = dim_meta.position
                dimension.role = dim_meta.role
                dimension.attributes = dict(dim_meta.attributes)
                changed = True
        dimensions += 1
        codes += _upsert_dimension_codes(session, dimension, dim_meta)

    session.flush()
    # Rows above were added through foreign keys, so the in-memory relationship
    # collections loaded earlier are stale. Expire them so callers that use the
    # returned dataset right away (e.g. ensure_series in the same transaction)
    # see the new dimensions and codes. Found live 2026-09-30: the first Turcat
    # run skipped every indicator because dataset.dimensions was still empty.
    for dimension in session.scalars(
        sa.select(DatasetDimension).where(DatasetDimension.dataset_id == dataset.id)
    ):
        session.expire(dimension, ["codes"])
    session.expire(dataset, ["dimensions"])
    outcome = UNCHANGED
    if changed:
        outcome = INSERTED if created else UPDATED
    return outcome, dataset


def _upsert_dimension_codes(
    session: Session, dimension: DatasetDimension, dim_meta: DimensionMeta
) -> int:
    incoming = {code.code: code for code in dim_meta.codes}
    existing = {code.code: code for code in dimension.codes}
    now_iso = datetime.now(UTC).isoformat()
    count = 0
    for code, code_meta in incoming.items():
        row = existing.get(code)
        if row is None:
            session.add(
                DimensionCode(
                    dimension_id=dimension.id,
                    code=code_meta.code,
                    label=code_meta.label,
                    parent_code=code_meta.parent_code,
                    is_default=code_meta.is_default,
                    attributes=dict(code_meta.attributes),
                )
            )
            count += 1
            continue
        if (
            row.label != code_meta.label
            or row.parent_code != code_meta.parent_code
            or row.is_default != code_meta.is_default
        ):
            row.label = code_meta.label
            row.parent_code = code_meta.parent_code
            row.is_default = code_meta.is_default
        # A code that reappears clears a previous ``removed_at`` marker, and any
        # other attribute change is persisted. A report-discovered code keeps its
        # marker even when the form lists it later.
        attributes = dict(code_meta.attributes)
        attributes.pop("removed_at", None)
        if (row.attributes or {}).get(DISCOVERED_FROM_REPORT):
            attributes.setdefault(DISCOVERED_FROM_REPORT, True)
            attributes.setdefault("first_seen", row.attributes.get("first_seen"))
        if row.attributes != attributes:
            row.attributes = attributes
        count += 1
    for code, row in existing.items():
        if code in incoming or row.attributes.get("removed_at"):
            continue
        # Codes learned from a generated report are not in the form lists the
        # walk sees, so the walk must not mark them removed.
        if (row.attributes or {}).get(DISCOVERED_FROM_REPORT):
            continue
        attributes = dict(row.attributes)
        attributes["removed_at"] = now_iso
        row.attributes = attributes
    session.flush()
    return count


def upsert_discovered_codes(
    session: Session,
    dataset: Dataset,
    codes: dict[str, str],
    *,
    first_seen: date | None = None,
) -> dict[str, int]:
    """Add report-only codes to a dataset's dimension code lists.

    A report may expose values for a catalogued dimension that its form options
    never list (TÜİK tourism income/expenditure categories). Each such code is
    stored with ``attributes['discovered_from_report']`` and
    ``attributes['first_seen']`` so a later catalog walk, which cannot see it,
    leaves it in place (see :func:`_upsert_dimension_codes`). Codes already
    catalogued are untouched; a previously removed discovered code is revived.
    Returns how many codes were added or revived per dimension.
    """
    seen = (first_seen or date.today()).isoformat()
    dimensions = {dimension.code: dimension for dimension in _ordered_dimensions(dataset)}
    added: dict[str, int] = {}
    for dimension_code, value in codes.items():
        dimension = dimensions.get(dimension_code)
        if dimension is None or not value:
            continue
        row = next((code for code in dimension.codes if code.code == value), None)
        if row is not None:
            if not (row.attributes or {}).get("removed_at"):
                continue
            attributes = dict(row.attributes)
            attributes.pop("removed_at", None)
            attributes[DISCOVERED_FROM_REPORT] = True
            attributes.setdefault("first_seen", seen)
            row.attributes = attributes
            added[dimension_code] = added.get(dimension_code, 0) + 1
            continue
        session.add(
            DimensionCode(
                dimension_id=dimension.id,
                code=value,
                label=value,
                attributes={DISCOVERED_FROM_REPORT: True, "first_seen": seen},
            )
        )
        added[dimension_code] = added.get(dimension_code, 0) + 1
    if added:
        session.flush()
        # New rows were added through the foreign key, so the loaded relationships
        # are stale; expire them so a same-transaction ``ensure_series`` sees the
        # discovered codes.
        for dimension in dimensions.values():
            session.expire(dimension, ["codes"])
        session.expire(dataset, ["dimensions"])
    return added


def sync_catalog(session: Session, connector: SourceConnector) -> CatalogSyncResult:
    """Upsert the connector's institution and every dataset it lists."""
    institution = upsert_institution(
        session, connector.institution_code, connector.institution_name
    )
    inserted = updated = unchanged = 0
    dimensions = codes = 0
    for meta in connector.list_datasets():
        outcome, _ = upsert_dataset(session, institution.id, meta)
        if outcome == INSERTED:
            inserted += 1
        elif outcome == UPDATED:
            updated += 1
        else:
            unchanged += 1
        dimensions += len(meta.dimensions)
        codes += sum(len(dimension.codes) for dimension in meta.dimensions)
    return CatalogSyncResult(
        institution_id=institution.id,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        dimensions=dimensions,
        codes=codes,
    )


@dataclass(frozen=True)
class SeriesDefinition:
    """The validated fields derived from a dataset plus one code per dimension."""

    external_code: str
    name: str
    frequency: str
    unit: str | None
    breakdown: dict[str, Any]
    dimension_codes: dict[str, str]
    attributes: dict[str, Any]


def _ordered_dimensions(dataset: Dataset) -> list[DatasetDimension]:
    return sorted(dataset.dimensions, key=lambda dimension: dimension.position)


def _code_label(dimension: DatasetDimension, code: str) -> str:
    for row in dimension.codes:
        if row.code == code:
            return row.label
    return code


def build_series_definition(dataset: Dataset, codes: dict[str, str]) -> SeriesDefinition:
    """Validate ``codes`` and derive the series fields (pure; no database writes)."""
    dimensions = _ordered_dimensions(dataset)
    non_time = [dimension for dimension in dimensions if dimension.role != ROLE_TIME]
    expected = {dimension.code for dimension in non_time}
    requested = set(codes)
    if expected != requested:
        missing = sorted(expected - requested)
        extra = sorted(requested - expected)
        raise SeriesDefinitionError(
            f"dataset {dataset.external_code!r} needs exactly one code for {sorted(expected)}; "
            f"missing={missing} extra={extra}"
        )
    for dimension in non_time:
        code = codes[dimension.code]
        valid = {
            row.code for row in dimension.codes if not (row.attributes or {}).get("removed_at")
        }
        if code not in valid:
            raise SeriesDefinitionError(
                f"code {code!r} is not valid for dimension {dimension.code!r} of "
                f"dataset {dataset.external_code!r}"
            )
    frequency = _frequency_from_codes(non_time, codes, dataset)
    unit = _unit_from_codes(non_time, codes)
    breakdown = {
        dimension.code: {
            "code": codes[dimension.code],
            "label": _code_label(dimension, codes[dimension.code]),
        }
        for dimension in non_time
    }
    detail = "; ".join(
        f"{dimension.label}: {_code_label(dimension, codes[dimension.code])}"
        for dimension in non_time
    )
    name = f"{dataset.name} — {detail}" if detail else dataset.name
    external_code = f"{dataset.external_code}:" + ".".join(
        codes[dimension.code] for dimension in non_time
    )
    attributes: dict[str, Any] = {"dataset_external_code": dataset.external_code}
    # Sources without a FREQ/AGG dimension (TCMB) carry the series aggregation on
    # the selected code; it becomes a series attribute the fetch reads.
    selected = _selected_code(non_time, codes)
    for dimension in non_time:
        row = selected.get(dimension.code)
        aggregation = (row.attributes or {}).get("aggregation") if row is not None else None
        if aggregation:
            attributes["aggregation"] = aggregation
            break
    return SeriesDefinition(
        external_code=external_code,
        name=name,
        frequency=frequency,
        unit=unit,
        breakdown=breakdown,
        dimension_codes={dimension.code: codes[dimension.code] for dimension in non_time},
        attributes=attributes,
    )


def _selected_code(
    dimensions: list[DatasetDimension], codes: dict[str, str]
) -> dict[str, DimensionCode]:
    selected: dict[str, DimensionCode] = {}
    for dimension in dimensions:
        code = codes.get(dimension.code)
        if code is None:
            continue
        for row in dimension.codes:
            if row.code == code:
                selected[dimension.code] = row
                break
    return selected


def _frequency_from_codes(
    dimensions: list[DatasetDimension],
    codes: dict[str, str],
    dataset: Dataset | None = None,
) -> str:
    """Derive the series frequency: FREQ dimension, then a code attribute, then
    the dataset default.

    Turcat INDICATOR codes carry ``attributes['frequency']``; CİP datasets publish
    one fixed frequency as ``attributes['default_frequency']``.
    """
    for dimension in dimensions:
        if dimension.code == "FREQ":
            try:
                return frequency_for_sdmx_code(codes[dimension.code])
            except Exception as exc:  # noqa: BLE001 - re-raise with context
                raise SeriesDefinitionError(f"unknown FREQ code {codes[dimension.code]!r}") from exc
    selected = _selected_code(dimensions, codes)
    for dimension in dimensions:
        row = selected.get(dimension.code)
        frequency = (row.attributes or {}).get("frequency") if row is not None else None
        if frequency:
            if frequency not in FREQUENCIES:
                raise SeriesDefinitionError(
                    f"unknown frequency attribute {frequency!r} on code {codes[dimension.code]!r}"
                )
            return frequency
    default = (dataset.attributes or {}).get("default_frequency") if dataset is not None else None
    if default:
        if default not in FREQUENCIES:
            raise SeriesDefinitionError(f"unknown default_frequency {default!r}")
        return str(default)
    raise SeriesDefinitionError(
        "dataset has no FREQ dimension, no frequency attribute and no default_frequency"
    )


def _unit_from_codes(dimensions: list[DatasetDimension], codes: dict[str, str]) -> str | None:
    for dimension in dimensions:
        if dimension.code == "UNIT_MEASURE":
            return _code_label(dimension, codes[dimension.code])
    selected = _selected_code(dimensions, codes)
    for dimension in dimensions:
        row = selected.get(dimension.code)
        unit = (row.attributes or {}).get("unit") if row is not None else None
        if unit:
            return str(unit)
    return None


def ensure_series(session: Session, dataset: Dataset, codes: dict[str, str]) -> Series:
    """Return the series row for ``(dataset, codes)``, creating it on first use."""
    definition = build_series_definition(dataset, codes)
    series = session.scalar(
        sa.select(Series).where(
            Series.institution_id == dataset.institution_id,
            Series.external_code == definition.external_code,
        )
    )
    if series is None:
        series = Series(
            institution_id=dataset.institution_id,
            dataset_id=dataset.id,
            external_code=definition.external_code,
            name=definition.name,
            frequency=definition.frequency,
            unit=definition.unit,
            source_category=dataset.source_category,
            breakdown=definition.breakdown,
            dimension_codes=definition.dimension_codes,
            attributes=definition.attributes,
        )
        session.add(series)
        session.flush()
        return series
    series.dataset_id = dataset.id
    series.name = definition.name
    series.frequency = definition.frequency
    series.unit = definition.unit
    series.source_category = dataset.source_category
    series.breakdown = definition.breakdown
    series.dimension_codes = definition.dimension_codes
    # Derived keys (today the aggregation) are refreshed on an existing row, but
    # other attribute keys are preserved: a changed aggregation must not be
    # ignored, yet a channel-specific key must not be dropped.
    merged = dict(series.attributes or {})
    merged.update(definition.attributes)
    series.attributes = merged
    session.flush()
    return series


def resolve_external_code(dataset: Dataset, external_code: str) -> dict[str, str] | None:
    """Split ``<dataset_external_code>:<codes in dimension order>`` using the dataset."""
    prefix, separator, key = external_code.partition(":")
    if not separator or prefix != dataset.external_code:
        return None
    non_time = [
        dimension.code for dimension in _ordered_dimensions(dataset) if dimension.role != ROLE_TIME
    ]
    # A single non-time dimension takes the whole remainder as its code: TCMB
    # series codes (TP.DK.USD.A.EF.YTL) contain dots, so splitting on them would
    # make every series unresolvable. With two or more dimensions the dotted
    # breakdown still applies.
    if len(non_time) == 1:
        if key == "":
            return None
        return {non_time[0]: key}
    parts = key.split(".")
    if len(parts) != len(non_time) or any(part == "" for part in parts):
        return None
    return dict(zip(non_time, parts, strict=True))


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one :func:`ingest_series` call."""

    series_id: int
    inserted: int
    unchanged: int
    point_count: int
    period_start: date | None
    period_end: date | None
    raw_object_key: str | None
    record: RecordResult


def ingest_series(
    session: Session,
    connector: SourceConnector,
    *,
    dataset: Dataset,
    codes: dict[str, str],
    start: date = date(2000, 1, 1),
    channel: str | None = None,
) -> IngestResult:
    """Fetch one series (dataset + codes) and append any changed observations.

    ``fetched_at`` is stamped at fetch time (UTC now, right after the network
    call returns), and observations point at the raw payload via
    ``raw_object_key``. The series coverage window is widened when the fetched
    points extend it.

    The fetch runs first so a connector can register report-only codes
    (:meth:`SourceConnector.register_discovered_codes`) before ``ensure_series``
    validates them; a failed fetch never pollutes the catalog.

    ``channel`` forces one fetch channel on connectors that support it (TÜİK:
    ``veriportali``); it is only passed through when set.
    """
    order = [
        dimension.code for dimension in _ordered_dimensions(dataset) if dimension.role != ROLE_TIME
    ]
    channel_kwargs: dict[str, Any] = {"channel": channel} if channel else {}
    fetched = connector.fetch_series(
        dataset.external_code, codes, order=order, start=start, **channel_kwargs
    )
    connector.register_discovered_codes(session, dataset, codes)
    series = ensure_series(session, dataset, codes)
    fetched_at = datetime.now(UTC)
    raw_key = fetched.raw_object_keys[0] if fetched.raw_object_keys else None
    result = record_observations(
        session,
        series.id,
        fetched.points,
        fetched_at=fetched_at,
        raw_object_key=raw_key,
        source_updated_at=fetched.source_updated_at,
    )

    periods = [period for period, _ in fetched.points]
    if periods:
        first, last = min(periods), max(periods)
        if series.coverage_start is None or first < series.coverage_start:
            series.coverage_start = first
        if series.coverage_end is None or last > series.coverage_end:
            series.coverage_end = last
    session.flush()

    return IngestResult(
        series_id=series.id,
        inserted=result.inserted,
        unchanged=result.unchanged,
        point_count=len(fetched.points),
        period_start=min(periods) if periods else None,
        period_end=max(periods) if periods else None,
        raw_object_key=raw_key,
        record=result,
    )


__all__ = [
    "CHANNEL_OWNED_ATTRIBUTES",
    "DISCOVERED_FROM_REPORT",
    "EMPTY",
    "ERROR_KINDS",
    "FORMAT_CHANGED",
    "INSERTED",
    "NOT_FOUND",
    "NO_CONNECTOR",
    "ROLE_FREQUENCY",
    "ROLE_GEO",
    "ROLE_OTHER",
    "ROLE_TIME",
    "SOURCE_ERROR",
    "THROTTLED",
    "TIMEOUT",
    "UNCHANGED",
    "UPDATED",
    "CatalogSyncResult",
    "ConnectorError",
    "DatasetMeta",
    "DimensionCodeMeta",
    "DimensionMeta",
    "FetchResult",
    "InMemoryObjectStore",
    "IngestResult",
    "MinioObjectStore",
    "ObjectStore",
    "SeriesDefinition",
    "SeriesMeta",
    "SourceConnector",
    "build_series_definition",
    "ensure_series",
    "ingest_series",
    "resolve_external_code",
    "store_raw",
    "sync_catalog",
    "upsert_dataset",
    "upsert_discovered_codes",
    "upsert_institution",
]
