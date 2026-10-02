"""TÜİK classification server (``siniflama.tuik.gov.tr``) connector.

The server publishes, token-free and without a WAF, one JSON endpoint per
classification *type* (``tur``), one full-tree endpoint per version, and the
correspondence tables between versions::

    GET /Classifications/GetSiniflamalarSahibi?tur=<tur>&searchname=null
    GET /Classifications/GetSiniflamaSatir?surumId=<Id>&seviye=1&kod=0
    GET /CorrespondenceTab/GetDonusumList?guncel=Y
    GET /CorrespondenceTab/GetDonusumDetayList?donusumId=<Id>&kaynakSurumId=...&hedefSurumId=...

The tree endpoint returns the *whole* tree (all levels) with ``ust_kod`` filled;
a targeted query with a specific ``kod`` returns ``ust_kod=null`` and must not be
used. Three versions (ids 23, 49, 435) have a source defect: a non-top row with an
empty ``ust_kod``. Those rows are kept and flagged ``parent_missing``; no parent
is invented.

Loaded rows are source-independent (``source='tuik_siniflama'``) and idempotent.
A version repeats ``code`` values, so an item is identified by ``(code,
parent_code, label)``; disappearing triples are marked ``attributes['removed_at']``
(never deleted), and a triple that reappears clears the marker.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    INSERTED,
    NOT_FOUND,
    SOURCE_ERROR,
    TIMEOUT,
    UNCHANGED,
    UPDATED,
    ConnectorError,
    ObjectStore,
    store_raw,
)
from app.connectors.tuik.client import RawResponse
from app.data.models import (
    Classification,
    ClassificationCorrespondence,
    ClassificationCorrespondenceItem,
    ClassificationItem,
    Dataset,
    DatasetDimension,
    DimensionClassificationLink,
    DimensionCode,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://siniflama.tuik.gov.tr"
INSTITUTION = "tuik"
SOURCE = "tuik_siniflama"
DATASET = "siniflama"

LIST_PATH = "/Classifications/GetSiniflamalarSahibi"
TREE_PATH = "/Classifications/GetSiniflamaSatir"
CORRESPONDENCE_LIST_PATH = "/CorrespondenceTab/GetDonusumList"
CORRESPONDENCE_DETAIL_PATH = "/CorrespondenceTab/GetDonusumDetayList"

# The types are scanned 0..30 and whichever answer a non-empty list are kept.
TUR_RANGE = range(0, 31)
TUR_LIST_CHANNEL = "siniflama-versions"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Bulk-insert batch size: 1000 rows x ~9 columns stays far below the PostgreSQL
# parameter limit and keeps a 35k-row version from taking minutes.
_BATCH = 1000


@dataclass(frozen=True)
class SiniflamaVersion:
    """One classification version from the per-type listing."""

    external_id: str
    type_code: str
    name: str
    name_en: str | None = None
    short_name: str | None = None
    short_name_en: str | None = None
    owner: str | None = None
    owner_en: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassificationItemRow:
    """One parsed row of a version's tree."""

    code: str
    parent_code: str | None
    level: int
    label: str
    label_en: str | None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassificationData:
    """A version plus its parsed tree and the raw payload key."""

    version: SiniflamaVersion
    items: list[ClassificationItemRow]
    raw_object_key: str | None = None


@dataclass(frozen=True)
class CorrespondenceSummary:
    """One correspondence table from the listing."""

    external_id: str
    name: str
    from_external_id: str | None = None
    to_external_id: str | None = None
    from_label: str | None = None
    to_label: str | None = None
    description: str | None = None
    description_en: str | None = None


@dataclass(frozen=True)
class CorrespondenceItemRow:
    """One from_code -> to_code row of a correspondence table."""

    from_code: str
    to_code: str
    from_label: str | None = None
    to_label: str | None = None


@dataclass(frozen=True)
class ClassificationLoad:
    """Outcome of loading one version."""

    external_id: str
    outcome: str
    items_inserted: int
    items_updated: int
    items_unchanged: int
    items_removed: int
    parent_missing: int


@dataclass(frozen=True)
class CorrespondenceLoad:
    """Outcome of loading one correspondence table."""

    external_id: str
    outcome: str
    rows: int
    from_linked: bool
    to_linked: bool


@dataclass(frozen=True)
class DimensionLinkRow:
    """One row of the ``--report`` TSV: a link or a near-miss for a dimension."""

    qualified: bool
    dataset: str
    dimension: str
    classification: str
    total: int
    coverage: float
    label_agreement: float


@dataclass(frozen=True)
class DimensionLinkResult:
    """Outcome of linking databrowser2 dimensions to classification versions."""

    dimensions: int
    links: int
    inserted: int
    updated: int
    deleted: int
    unchanged: int
    report: tuple[DimensionLinkRow, ...] = ()


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def parse_version_list(payload: Any, *, type_code: str) -> list[SiniflamaVersion]:
    """Parse one ``GetSiniflamalarSahibi`` response (an empty list is valid)."""
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, "classification list is not a JSON array")
    versions: list[SiniflamaVersion] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("Id") in (None, ""):
            continue
        attributes: dict[str, Any] = {}
        for key in ("Href", "Img", "Tur_Sira_No"):
            value = row.get(key)
            if value not in (None, ""):
                attributes[key.lower()] = value
        versions.append(
            SiniflamaVersion(
                external_id=str(row["Id"]),
                type_code=str(type_code),
                name=str(row.get("Ad") or row.get("Kisa_Ad") or row["Id"]),
                name_en=_opt_str(row.get("Ad_en")),
                short_name=_opt_str(row.get("Kisa_Ad")),
                short_name_en=_opt_str(row.get("Kisa_Ad_en")),
                owner=_opt_str(row.get("Sahibi")),
                owner_en=_opt_str(row.get("Sahibi_en")),
                attributes=attributes,
            )
        )
    return versions


def _item_attributes(row: Mapping[str, Any], *, parent_missing: bool) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    source_row_id = _opt_id(row.get("satirId"))
    if source_row_id is not None:
        # Every source row has a distinct satirId, but its stability across
        # refreshes is unknown, so it is provenance, not the item identity.
        attributes["source_row_id"] = source_row_id
    unit = _opt_str(row.get("olcuBirimKodu"))
    if unit is not None:
        attributes["unit_code"] = unit
    for index in range(1, 6):
        value = row.get(f"detay{index}")
        if value not in (None, ""):
            attributes[f"detay{index}"] = value
    for index in range(1, 11):
        value = row.get(f"segment{index}")
        if value not in (None, ""):
            attributes[f"segment{index}"] = value
    if parent_missing:
        attributes["parent_missing"] = True
    return attributes


def parse_tree(payload: Any, *, raw_object_key: str | None = None) -> list[ClassificationItemRow]:
    """Parse a full-tree response into item rows.

    A non-top row whose ``ust_kod`` is empty is flagged ``parent_missing`` and
    kept with ``parent_code=None``; the source defect is never repaired by
    guessing a parent.
    """
    if not isinstance(payload, list):
        raise ConnectorError(
            FORMAT_CHANGED,
            "classification tree is not a JSON array",
            raw_object_key=raw_object_key,
        )
    if not payload:
        raise ConnectorError(EMPTY, "classification tree is empty", raw_object_key=raw_object_key)
    items: list[ClassificationItemRow] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("kod") in (None, ""):
            continue
        code = str(row["kod"]).strip()
        if not code:
            continue
        parent_code = _opt_str(row.get("ust_kod"))
        try:
            level = int(row.get("duzey") or 0)
        except (TypeError, ValueError) as exc:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"tree row {code!r} has a non-numeric duzey",
                raw_object_key=raw_object_key,
            ) from exc
        parent_missing = parent_code is None and level > 1
        items.append(
            ClassificationItemRow(
                code=code,
                parent_code=parent_code,
                level=level,
                label=str(row.get("tanim") or code),
                label_en=_opt_str(row.get("tanim_en")),
                attributes=_item_attributes(row, parent_missing=parent_missing),
            )
        )
    return items


def parse_correspondence_list(payload: Any) -> list[CorrespondenceSummary]:
    """Parse a ``GetDonusumList`` response."""
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, "correspondence list is not a JSON array")
    summaries: list[CorrespondenceSummary] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("Id") in (None, ""):
            continue
        summaries.append(
            CorrespondenceSummary(
                external_id=str(row["Id"]),
                name=str(row.get("DonusumAdi") or row["Id"]),
                from_external_id=_opt_id(row.get("KaynakSurumId")),
                to_external_id=_opt_id(row.get("HedefSurumId")),
                from_label=_opt_str(row.get("KaynakSurum")),
                to_label=_opt_str(row.get("HedefSurum")),
                description=_opt_str(row.get("Aciklama")),
                description_en=_opt_str(row.get("Aciklama_en")),
            )
        )
    return summaries


def parse_correspondence_detail(payload: Any) -> list[CorrespondenceItemRow]:
    """Parse a ``GetDonusumDetayList`` response.

    ``KaynakKod``/``HedefKod`` may be ``-`` ("no source code") or ``*`` ("any");
    they are stored verbatim, never dropped.
    """
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, "correspondence detail is not a JSON array")
    items: list[CorrespondenceItemRow] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        from_code = row.get("KaynakKod")
        to_code = row.get("HedefKod")
        if from_code in (None, "") and to_code in (None, ""):
            continue
        items.append(
            CorrespondenceItemRow(
                from_code="" if from_code in (None, "") else str(from_code),
                to_code="" if to_code in (None, "") else str(to_code),
                from_label=_opt_str(row.get("KaynakSurum")),
                to_label=_opt_str(row.get("HedefSurum")),
            )
        )
    return items


class SiniflamaClient:
    """Retrying, raw-storing HTTP client for the classification server."""

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
        self._timeout_s = resolved.siniflama_request_timeout_s
        self._max_concurrency = resolved.siniflama_max_concurrency
        self._max_retries = resolved.siniflama_max_retries
        self._retry_backoff_s = resolved.siniflama_retry_backoff_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._headers = {"X-Requested-With": "XMLHttpRequest", "User-Agent": USER_AGENT}
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
            logger.warning("siniflama raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    @staticmethod
    def _error_message(content: bytes) -> str:
        return content[:300].decode("utf-8", errors="replace")

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    def _request(self, path: str, *, params: Mapping[str, Any], dataset: str, channel: str):
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            try:
                response = self._client.get(url, params=params, headers=self._headers)
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT,
                        f"siniflama {path} timed out after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning("siniflama timeout on %s (attempt %d)", path, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"siniflama transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning("siniflama transport error on %s (attempt %d)", path, attempt + 1)
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
                    "siniflama HTTP %d on %s (attempt %d)",
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

    def _raw(self, response: httpx.Response, channel: str, ext: str = "json") -> RawResponse:
        key = self._store_raw(DATASET, channel, response.content, ext)
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def version_list(self, tur: int) -> RawResponse:
        """List the versions of one classification type."""
        response = self._request(
            LIST_PATH,
            params={"tur": tur, "searchname": "null"},
            dataset=DATASET,
            channel=f"{TUR_LIST_CHANNEL}-{tur}",
        )
        return self._raw(response, f"{TUR_LIST_CHANNEL}-{tur}")

    def tree(self, version_id: str) -> RawResponse:
        """Fetch the whole tree of one version."""
        response = self._request(
            TREE_PATH,
            params={"surumId": version_id, "seviye": 1, "kod": 0},
            dataset=DATASET,
            channel=str(version_id),
        )
        return self._raw(response, str(version_id))

    def correspondence_list(self) -> RawResponse:
        """Fetch every current correspondence table."""
        response = self._request(
            CORRESPONDENCE_LIST_PATH,
            params={"guncel": "Y"},
            dataset=DATASET,
            channel="correspondences",
        )
        return self._raw(response, "correspondences")

    def correspondence_detail(
        self, donusum_id: str, kaynak_surum_id: str | None, hedef_surum_id: str | None
    ) -> RawResponse:
        """Fetch one correspondence table's code rows."""
        response = self._request(
            CORRESPONDENCE_DETAIL_PATH,
            params={
                "donusumId": donusum_id,
                "kaynakSurumId": kaynak_surum_id,
                "hedefSurumId": hedef_surum_id,
            },
            dataset=DATASET,
            channel=f"correspondence-{donusum_id}",
        )
        return self._raw(response, f"correspondence-{donusum_id}")


def discover_versions(
    client: SiniflamaClient,
    *,
    turs: Iterable[int] = TUR_RANGE,
) -> tuple[list[SiniflamaVersion], list[tuple[str, ConnectorError]]]:
    """Scan every type and return the non-empty versions plus per-type failures."""
    versions: list[SiniflamaVersion] = []
    failures: list[tuple[str, ConnectorError]] = []
    for tur in turs:
        try:
            response = client.version_list(tur)
            versions.extend(parse_version_list(response.json(), type_code=str(tur)))
        except ConnectorError as exc:
            failures.append((str(tur), exc))
    return versions, failures


def fetch_classification(client: SiniflamaClient, version: SiniflamaVersion) -> ClassificationData:
    """Fetch and parse one version's full tree."""
    response = client.tree(version.external_id)
    items = parse_tree(response.json(), raw_object_key=response.raw_object_key)
    return ClassificationData(version=version, items=items, raw_object_key=response.raw_object_key)


def _item_identity(code: str, parent_code: str | None, label: str) -> tuple[str, str | None, str]:
    """The item identity: the source repeats codes, so the triple is the key."""
    return (code, parent_code, label)


def _plan_item_changes(
    classification_id: int,
    incoming: Sequence[ClassificationItemRow],
    existing: Sequence[Any],
) -> tuple[list[dict[str, Any]], int, int, int, list[Any]]:
    """Decide which items to upsert and which existing rows to mark removed.

    Pure: returns ``(upsert_values, inserted, updated, unchanged, removed_rows)``.
    An item is identified by ``(code, parent_code, label)``; an identical row
    repeated in the payload collapses to one. A row whose stored ``removed_at``
    marker is cleared counts as updated.
    """
    inserted = updated = unchanged = 0
    upsert_rows: list[dict[str, Any]] = []
    incoming_by_identity: dict[tuple[str, str | None, str], ClassificationItemRow] = {}
    for row in incoming:
        incoming_by_identity[_item_identity(row.code, row.parent_code, row.label)] = row
    existing_by_identity = {
        _item_identity(row.code, row.parent_code, row.label): row for row in existing
    }
    for identity, row in incoming_by_identity.items():
        values = {
            "classification_id": classification_id,
            "code": row.code,
            "parent_code": row.parent_code,
            "level": row.level,
            "label": row.label,
            "label_en": row.label_en,
            "attributes": dict(row.attributes),
        }
        previous = existing_by_identity.get(identity)
        if previous is None:
            inserted += 1
            upsert_rows.append(values)
            continue
        previous_attributes = dict(previous.attributes or {})
        removed_marker = previous_attributes.pop("removed_at", None)
        changed = (
            previous.level != row.level
            or previous.label_en != row.label_en
            or previous_attributes != values["attributes"]
            or removed_marker is not None
        )
        if changed:
            updated += 1
            upsert_rows.append(values)
        else:
            unchanged += 1
    removed_rows = [
        previous
        for identity, previous in existing_by_identity.items()
        if identity not in incoming_by_identity and "removed_at" not in (previous.attributes or {})
    ]
    return upsert_rows, inserted, updated, unchanged, removed_rows


def _upsert_classification_items(
    session: Session, classification_id: int, rows: Sequence[ClassificationItemRow], now: datetime
) -> tuple[int, int, int, int]:
    """Upsert every item; mark missing existing rows ``removed_at``."""
    now_iso = now.isoformat()
    existing_rows = session.execute(
        sa.select(
            ClassificationItem.id,
            ClassificationItem.code,
            ClassificationItem.parent_code,
            ClassificationItem.level,
            ClassificationItem.label,
            ClassificationItem.label_en,
            ClassificationItem.attributes,
        ).where(ClassificationItem.classification_id == classification_id)
    ).all()

    upsert_rows, inserted, updated, unchanged, removed_rows = _plan_item_changes(
        classification_id, rows, existing_rows
    )

    for start in range(0, len(upsert_rows), _BATCH):
        batch = upsert_rows[start : start + _BATCH]
        statement = pg_insert(ClassificationItem).values(batch)
        statement = statement.on_conflict_do_update(
            index_elements=["classification_id", "code", "parent_code", "label"],
            set_={
                "level": statement.excluded.level,
                "label_en": statement.excluded.label_en,
                "attributes": statement.excluded.attributes,
            },
        )
        session.execute(statement)

    if removed_rows:
        # Core-level executemany: the ORM bulk path refuses a WHERE on a bindparam.
        table = ClassificationItem.__table__
        session.execute(
            sa.update(table)
            .where(table.c.id == sa.bindparam("item_id"))
            .values(attributes=sa.bindparam("attrs")),
            [
                {
                    "item_id": row.id,
                    "attrs": {**(row.attributes or {}), "removed_at": now_iso},
                }
                for row in removed_rows
            ],
        )
    session.flush()
    return inserted, updated, unchanged, len(removed_rows)


def load_classifications(
    session: Session,
    datas: Iterable[ClassificationData],
    *,
    source: str = SOURCE,
) -> list[ClassificationLoad]:
    """Idempotently upsert parsed versions and their items (never delete)."""
    loads: list[ClassificationLoad] = []
    for data in datas:
        version = data.version
        now = datetime.now(UTC)
        row = session.scalar(
            sa.select(Classification).where(
                Classification.source == source,
                Classification.external_id == version.external_id,
            )
        )
        created = row is None
        if row is None:
            row = Classification(
                source=source,
                external_id=version.external_id,
                type_code=version.type_code,
                name=version.name,
            )
            session.add(row)
            session.flush()
        changed = created
        for field_name, value in (
            ("type_code", version.type_code),
            ("short_name", version.short_name),
            ("short_name_en", version.short_name_en),
            ("name", version.name),
            ("name_en", version.name_en),
            ("owner", version.owner),
            ("owner_en", version.owner_en),
            ("attributes", version.attributes),
        ):
            if getattr(row, field_name) != value:
                setattr(row, field_name, value)
                changed = True
        row.fetched_at = now
        inserted, updated, unchanged, removed = _upsert_classification_items(
            session, row.id, data.items, now
        )
        loads.append(
            ClassificationLoad(
                external_id=version.external_id,
                outcome=INSERTED if created else (UPDATED if changed else UNCHANGED),
                items_inserted=inserted,
                items_updated=updated,
                items_unchanged=unchanged,
                items_removed=removed,
                parent_missing=sum(
                    1 for item in data.items if item.attributes.get("parent_missing")
                ),
            )
        )
    session.flush()
    return loads


def _upsert_correspondence_items(
    session: Session, correspondence_id: int, rows: Sequence[CorrespondenceItemRow]
) -> int:
    deduped: dict[tuple[str, str], CorrespondenceItemRow] = {}
    for row in rows:
        deduped[(row.from_code, row.to_code)] = row
    values = [
        {
            "correspondence_id": correspondence_id,
            "from_code": from_code,
            "to_code": to_code,
            "from_label": row.from_label,
            "to_label": row.to_label,
        }
        for (from_code, to_code), row in deduped.items()
    ]
    for start in range(0, len(values), _BATCH):
        batch = values[start : start + _BATCH]
        statement = pg_insert(ClassificationCorrespondenceItem).values(batch)
        statement = statement.on_conflict_do_update(
            index_elements=["correspondence_id", "from_code", "to_code"],
            set_={
                "from_label": statement.excluded.from_label,
                "to_label": statement.excluded.to_label,
            },
        )
        session.execute(statement)
    return len(values)


def load_correspondences(
    session: Session,
    entries: Iterable[tuple[CorrespondenceSummary, Sequence[CorrespondenceItemRow]]],
    *,
    source: str = SOURCE,
) -> list[CorrespondenceLoad]:
    """Idempotently upsert correspondence tables and their code rows."""
    classification_ids = {
        external_id: row_id
        for external_id, row_id in session.execute(
            sa.select(Classification.external_id, Classification.id).where(
                Classification.source == source
            )
        )
    }
    loads: list[CorrespondenceLoad] = []
    for summary, items in entries:
        from_id = (
            classification_ids.get(summary.from_external_id)
            if summary.from_external_id is not None
            else None
        )
        to_id = (
            classification_ids.get(summary.to_external_id)
            if summary.to_external_id is not None
            else None
        )
        attributes = {
            "from_external_id": summary.from_external_id,
            "to_external_id": summary.to_external_id,
            "from_label": summary.from_label,
            "to_label": summary.to_label,
            "description": summary.description,
            "description_en": summary.description_en,
        }
        row = session.scalar(
            sa.select(ClassificationCorrespondence).where(
                ClassificationCorrespondence.source == source,
                ClassificationCorrespondence.external_id == summary.external_id,
            )
        )
        created = row is None
        if row is None:
            row = ClassificationCorrespondence(
                source=source, external_id=summary.external_id, name=summary.name
            )
            session.add(row)
            session.flush()
        changed = created
        for field_name, value in (
            ("name", summary.name),
            ("from_classification_id", from_id),
            ("to_classification_id", to_id),
            ("attributes", attributes),
        ):
            if getattr(row, field_name) != value:
                setattr(row, field_name, value)
                changed = True
        count = _upsert_correspondence_items(session, row.id, items)
        loads.append(
            CorrespondenceLoad(
                external_id=summary.external_id,
                outcome=INSERTED if created else (UPDATED if changed else UNCHANGED),
                rows=count,
                from_linked=from_id is not None,
                to_linked=to_id is not None,
            )
        )
    session.flush()
    return loads


# Dimension codes that are totals/aggregates rather than classification members.
_AGGREGATE_CODES = {"_T", "_Z", "TOTAL", "_X"}


def is_aggregate_code(code: str) -> bool:
    """True for the dimension codes ignored when matching a classification."""
    return code in _AGGREGATE_CODES or code.startswith("_")


# Matching thresholds: a dimension links to a version only when it has at least
# ``_MIN_TOTAL`` meaningful codes, covers at least ``_MIN_COVERAGE`` of them and
# at least ``_MIN_LABEL_AGREEMENT`` of the found codes carry the version's label.
_MIN_TOTAL = 3
_MIN_COVERAGE = 0.8
_MIN_LABEL_AGREEMENT = 0.8

# How a code is normalized before matching; recorded on every link's attributes.
NORMALIZATION = "strip_dots_section_prefix"

# A dimension whose own code names a classification is reported even when it
# does not link (the near-misses of the ``--report`` TSV).
_CLASSIFICATION_MARKERS = (
    "COICOP",
    "NACE",
    "ACTIVITY",
    "CPA",
    "ISIC",
    "SITC",
    "HS",
    "BEC",
    "COFOG",
    "ISCO",
)

_SECTION_PREFIX = re.compile(r"^([A-Z])(\d+)$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# NFKD strips most accents; Turkish dotless ``ı``/``İ`` need an explicit fold.
_LETTER_FOLD = str.maketrans({"ı": "i", "İ": "I"})


def normalize_classification_code(code: str) -> str:
    """Normalize a classification/dimension code for matching.

    Dots are removed (``01.1.1`` -> ``0111``) and a section letter prefixed to
    digits is dropped (``B05`` -> ``05``, ``B0510`` -> ``0510``). A bare section
    letter (``B``) and a hyphenated range (``B-C-D``) stay unchanged; both sides
    of a match are normalized the same way.
    """
    normalized = code.strip().replace(".", "")
    match = _SECTION_PREFIX.match(normalized)
    return match.group(2) if match else normalized


def normalize_label(label: str | None) -> str:
    """Lower-case, strip accents and collapse non-alphanumerics to single spaces."""
    if not label:
        return ""
    decomposed = unicodedata.normalize("NFKD", label.translate(_LETTER_FOLD))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NON_ALNUM.sub(" ", stripped.casefold()).strip()


def looks_like_classification(dimension_code: str) -> bool:
    """True when a dimension's own code names a known classification."""
    upper = dimension_code.upper()
    return any(marker in upper for marker in _CLASSIFICATION_MARKERS)


@dataclass(frozen=True)
class ClassificationMatch:
    """One dimension's score against one classification version."""

    classification_id: int
    total: int
    found: int
    coverage: float
    label_agreement: float

    @property
    def qualified(self) -> bool:
        """True when the match clears the total/coverage/label thresholds."""
        return (
            self.total >= _MIN_TOTAL
            and self.coverage >= _MIN_COVERAGE
            and self.label_agreement >= _MIN_LABEL_AGREEMENT
        )


def dimension_classification_matches(
    codes: Iterable[str],
    labels: Mapping[str, str],
    *,
    class_labels: Mapping[int, Mapping[str, set[str]]],
    inverted: Mapping[str, set[int]],
) -> list[ClassificationMatch]:
    """Score one dimension against every version that shares a normalized code.

    Candidates come from ``inverted`` (normalized code -> versions), so only
    versions sharing at least one code are scored instead of every
    dimension x version pair. ``class_labels`` maps a version to its normalized
    code -> normalized labels. Results are ordered by
    ``(label_agreement, coverage)`` descending.
    """
    meaningful = [code for code in codes if not is_aggregate_code(code)]
    total = len(meaningful)
    if not total:
        return []
    found: Counter[int] = Counter()
    agreed: Counter[int] = Counter()
    for code in meaningful:
        normalized = normalize_classification_code(code)
        versions = inverted.get(normalized)
        if not versions:
            continue
        label = normalize_label(labels.get(code))
        for classification_id in versions:
            found[classification_id] += 1
            if label and label in class_labels[classification_id].get(normalized, frozenset()):
                agreed[classification_id] += 1
    matches = [
        ClassificationMatch(
            classification_id=classification_id,
            total=total,
            found=count,
            coverage=count / total,
            label_agreement=agreed[classification_id] / count,
        )
        for classification_id, count in found.items()
    ]
    matches.sort(key=lambda match: (match.label_agreement, match.coverage), reverse=True)
    return matches


def stale_link_keys(
    existing: Mapping[tuple[int, int], Any],
    linked: Mapping[tuple[int, int], Any],
) -> list[tuple[int, int]]:
    """Keys present before a run but not produced by it; these links are deleted."""
    return [key for key in existing if key not in linked]


def _classification_label_maps(
    session: Session,
) -> tuple[dict[int, dict[str, set[str]]], dict[str, set[int]]]:
    """Build once: version -> normalized code -> labels, and the inverted index."""
    class_labels: dict[int, dict[str, set[str]]] = {}
    inverted: dict[str, set[int]] = {}
    statement = sa.select(
        ClassificationItem.classification_id,
        ClassificationItem.code,
        ClassificationItem.label,
        ClassificationItem.label_en,
    ).execution_options(stream_results=True, yield_per=10_000)
    for classification_id, code, label, label_en in session.execute(statement):
        normalized = normalize_classification_code(code)
        bucket = class_labels.setdefault(classification_id, {}).setdefault(normalized, set())
        for value in (label_en, label):
            normalized_label = normalize_label(value)
            if normalized_label:
                bucket.add(normalized_label)
        inverted.setdefault(normalized, set()).add(classification_id)
    return class_labels, inverted


def link_dimensions(
    session: Session, *, dry_run: bool = False, collect_report: bool = False
) -> DimensionLinkResult:
    """Link databrowser2 dimensions to classification versions by normalized scores.

    A dimension links to a version when at least ``_MIN_TOTAL`` non-aggregate
    codes exist in it (``coverage``) and at least ``_MIN_LABEL_AGREEMENT`` of the
    found codes carry the version's label. Several versions may qualify and all
    are kept. The table is derived data, recomputed from scratch on every run, so
    links that no longer qualify (including links of dimensions that are gone)
    are deleted; ``created_at`` of a surviving row is preserved.
    """
    class_labels, inverted = _classification_label_maps(session)

    dimension_codes: dict[int, dict[str, str]] = {}
    dimension_meta: dict[int, tuple[str, str]] = {}
    for dimension_id, dataset_code, dimension_code, code, label, attributes in session.execute(
        sa.select(
            DimensionCode.dimension_id,
            Dataset.external_code,
            DatasetDimension.code,
            DimensionCode.code,
            DimensionCode.label,
            DimensionCode.attributes,
        )
        .join(DatasetDimension, DatasetDimension.id == DimensionCode.dimension_id)
        .join(Dataset, Dataset.id == DatasetDimension.dataset_id)
        .where(Dataset.attributes["channel"].astext == "databrowser2")
    ):
        if (attributes or {}).get("removed_at"):
            continue
        dimension_codes.setdefault(dimension_id, {})[code] = label
        dimension_meta[dimension_id] = (dataset_code, dimension_code)

    classification_names = {
        classification_id: short_name or name
        for classification_id, short_name, name in session.execute(
            sa.select(Classification.id, Classification.short_name, Classification.name)
        )
    }

    existing = {
        (link.dimension_id, link.classification_id): link
        for link in session.scalars(sa.select(DimensionClassificationLink))
    }

    inserted = updated = unchanged = deleted = links = 0
    linked: dict[tuple[int, int], Any] = {}
    report: list[DimensionLinkRow] = []
    for dimension_id, codes in dimension_codes.items():
        dataset_code, dimension_code = dimension_meta[dimension_id]
        matches = dimension_classification_matches(
            codes, codes, class_labels=class_labels, inverted=inverted
        )
        class_like = collect_report and looks_like_classification(dimension_code)
        near_miss = 0
        for match in matches:
            if not match.qualified:
                if class_like and near_miss < 5:
                    near_miss += 1
                    report.append(
                        DimensionLinkRow(
                            qualified=False,
                            dataset=dataset_code,
                            dimension=dimension_code,
                            classification=classification_names.get(
                                match.classification_id, str(match.classification_id)
                            ),
                            total=match.total,
                            coverage=match.coverage,
                            label_agreement=match.label_agreement,
                        )
                    )
                continue
            links += 1
            key = (dimension_id, match.classification_id)
            linked[key] = match
            link = existing.get(key)
            values = {
                "matched_codes": match.found,
                "total_codes": match.total,
                "coverage": match.coverage,
                "label_agreement": match.label_agreement,
                "attributes": {"normalization": NORMALIZATION},
            }
            if link is None:
                inserted += 1
                if not dry_run:
                    session.add(
                        DimensionClassificationLink(
                            dimension_id=dimension_id,
                            classification_id=match.classification_id,
                            **values,
                        )
                    )
            elif (
                link.matched_codes != match.found
                or link.total_codes != match.total
                or link.coverage != match.coverage
                or link.label_agreement != match.label_agreement
                or (link.attributes or {}).get("normalization") != NORMALIZATION
            ):
                updated += 1
                if not dry_run:
                    for field_name, value in values.items():
                        setattr(link, field_name, value)
            else:
                unchanged += 1
            if collect_report:
                report.append(
                    DimensionLinkRow(
                        qualified=True,
                        dataset=dataset_code,
                        dimension=dimension_code,
                        classification=classification_names.get(
                            match.classification_id, str(match.classification_id)
                        ),
                        total=match.total,
                        coverage=match.coverage,
                        label_agreement=match.label_agreement,
                    )
                )

    stale = stale_link_keys(existing, linked)
    deleted = len(stale)
    if not dry_run:
        for key in stale:
            session.delete(existing[key])
        session.flush()
    if collect_report:
        report.sort(key=lambda row: (row.label_agreement, row.coverage), reverse=True)
    return DimensionLinkResult(
        dimensions=len(dimension_codes),
        links=links,
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged,
        report=tuple(report),
    )


def classification_codes_for(
    session: Session, dimension_id: int, dimension_code: str
) -> list[ClassificationItem]:
    """Return the linked item(s) whose code matches one dimension code.

    Uses the same normalization as linking, so ``B05`` finds the version's ``05``
    and ``0111`` finds ``01.1.1``. Only the classifications already linked to the
    dimension are searched (used later for Turkish labels / hierarchy).
    """
    classification_ids = session.scalars(
        sa.select(DimensionClassificationLink.classification_id).where(
            DimensionClassificationLink.dimension_id == dimension_id
        )
    ).all()
    if not classification_ids:
        return []
    normalized = normalize_classification_code(dimension_code)
    items = session.scalars(
        sa.select(ClassificationItem)
        .where(ClassificationItem.classification_id.in_(classification_ids))
        .execution_options(stream_results=True, yield_per=10_000)
    )
    return [item for item in items if normalize_classification_code(item.code) == normalized]


__all__ = [
    "DEFAULT_BASE_URL",
    "DATASET",
    "INSTITUTION",
    "NORMALIZATION",
    "SOURCE",
    "ClassificationData",
    "ClassificationItemRow",
    "ClassificationLoad",
    "ClassificationMatch",
    "CorrespondenceItemRow",
    "CorrespondenceLoad",
    "CorrespondenceSummary",
    "DimensionLinkResult",
    "DimensionLinkRow",
    "SiniflamaClient",
    "SiniflamaVersion",
    "classification_codes_for",
    "dimension_classification_matches",
    "discover_versions",
    "fetch_classification",
    "is_aggregate_code",
    "link_dimensions",
    "load_classifications",
    "load_correspondences",
    "looks_like_classification",
    "normalize_classification_code",
    "normalize_label",
    "parse_correspondence_detail",
    "parse_correspondence_list",
    "parse_tree",
    "parse_version_list",
]
