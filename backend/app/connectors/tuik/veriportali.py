"""TÜİK Veri Portalı (``veriportali.tuik.gov.tr``) connector.

The portal is the modern catalogue behind TÜİK's databrowser2 UI. It exposes a
flat JSON dataflow catalogue plus whole-dataset SDMX-JSON/CSV/XML downloads and
the press-release catalogue/detail used by ``data.tuik.gov.tr``. Measured live
2026-10-02: the WAF only inspects headers, so every JSON request must carry a
browser ``User-Agent`` *and* ``X-Requested-With: XMLHttpRequest`` (no UA -> HTTP
403 ``text/plain``; UA without XHR -> HTTP 404 ``text/plain`` on JSON paths).
JSON APIs showed no throttling up to 8 parallel, so one worker with no fixed
pause is used; a 200 ``text/html`` "Yönlendiriliyor" page still means throttling
and is retried with backoff.

Three channels live here:

- the dataflow catalogue, merged onto the existing TÜİK datasets under
  ``attributes['veriportali']`` (never touching their dimensions),
- the back-up whole-dataset fetcher used by
  :meth:`~app.connectors.tuik.connector.TuikConnector.fetch_series` when both
  databrowser2 and nsiws fail,
- the press-release catalogue (``documents`` rows, ``source='tuik_press'``) and
  the on-demand detail fetch that fills ``documents.content_text`` and writes the
  bulletin -> dataset links (Excel/PDF files are never downloaded).

Everything is synchronous, network-free in its parsers, and idempotent in its
loaders. Rows are never deleted: one that disappears gets a ``removed_at`` marker
(only after a COMPLETE crawl) and a reappearing row clears it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    INSERTED,
    NOT_FOUND,
    SOURCE_ERROR,
    THROTTLED,
    TIMEOUT,
    ConnectorError,
    DatasetMeta,
    FetchResult,
    ObjectStore,
    store_raw,
    upsert_dataset,
)
from app.connectors.tuik.parsers import is_throttle_response, points_from_sdmx_json
from app.connectors.tuik.yayin import DocumentLoad, load_documents
from app.data.models import Dataset, Document, DocumentDatasetLink

logger = logging.getLogger(__name__)

INSTITUTION = "tuik"
SOURCE = "tuik_press"
DOC_TYPE = "Haber Bülteni"
CHANNEL = "veriportali"
# First year the press calendar lists TÜİK releases (2004 and earlier are empty).
PRESS_FIRST_YEAR = 2005
# ``documents.attributes`` key filled by the on-demand detail fetch.
PRESS_ATTRIBUTE = "press"
VERIPORTALI_ATTRIBUTE = "veriportali"

CATALOG_CHANNEL = "veriportali-catalog"
FILE_CHANNEL = "veriportali-file"
PRESS_LIST_CHANNEL = "veriportali-press-list"
PRESS_CHANNEL = "veriportali-press"
CALENDAR_CHANNEL = "veriportali-calendar"

DATAFLOWS_PATH = "/api/tr/dataflows"
PRESS_LIST_PATH = "/api/tr/press"
CALENDAR_PATH = "/Kurumsal/GetYillikHaberBulteniListesi"

JSON_ACCEPT = "application/json, text/plain, */*"

# Attribute keys of a portal record whose change means the record changed; the
# ``seen_at`` stamp is bumped on every run and must not count as an update.
_VOLATILE_ATTRIBUTES = frozenset({"seen_at"})


@dataclass(frozen=True)
class RawResponse:
    """One successful portal HTTP response plus where its body was stored."""

    status_code: int
    content: bytes
    headers: Mapping[str, str]
    raw_object_key: str | None

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    def json(self) -> Any:
        return parse_json(self.content, raw_object_key=self.raw_object_key)


def parse_json(content: bytes, *, raw_object_key: str | None = None) -> Any:
    """Parse a JSON body, mapping a decode failure to ``format_changed``."""
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConnectorError(
            FORMAT_CHANGED,
            "veriportali response body is not valid JSON",
            raw_object_key=raw_object_key,
        ) from exc


# --- dataflow catalogue -----------------------------------------------------


@dataclass(frozen=True)
class DataflowRecord:
    """One dataflow of the portal catalogue (SDMX id plus its presentation)."""

    portal_id: str
    external_code: str
    version: str
    name: str
    description: str | None
    period: str | None
    updated_at: str | None
    downloadable: bool
    category_path: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)

    def attributes(self, *, seen_at: str) -> dict[str, Any]:
        """The ``attributes['veriportali']`` object stored on the dataset."""
        return {
            "id": self.portal_id,
            "version": self.version,
            "updated_at": self.updated_at,
            "period": self.period,
            "downloadable": self.downloadable,
            "category_path": list(self.category_path),
            "footnotes": list(self.footnotes),
            "description": self.description,
            "seen_at": seen_at,
        }


def split_portal_id(portal_id: str) -> tuple[str, str]:
    """Split ``DF_X+V1.0`` into ``("DF_X", "1.0")`` (the ``+`` is the version's)."""
    code, separator, rest = portal_id.partition("+")
    if not separator:
        return portal_id, ""
    version = rest[1:] if rest[:1] in ("V", "v") else rest
    return code or portal_id, version


def _version_key(version: str | None) -> tuple[int, ...]:
    parts = []
    for part in (version or "").split("."):
        parts.append(int(part) if part.isdigit() else -1)
    return tuple(parts)


def select_dataflow(
    records: Iterable[DataflowRecord], *, version: str | None = None
) -> DataflowRecord | None:
    """Pick one record of a code the portal lists in several versions.

    The record whose version equals ``version`` (our catalogued version) wins;
    otherwise the highest version. Never depends on the portal's list order.
    """
    candidates = list(records)
    if not candidates:
        return None
    if version is not None:
        for record in candidates:
            if record.version == version:
                return record
    return max(candidates, key=lambda record: _version_key(record.version))


def _category_path(category: Any) -> list[str]:
    path: list[str] = []
    node = category
    while isinstance(node, dict) and node.get("name"):
        path.append(str(node["name"]))
        node = node.get("child")
    return path


def parse_dataflow_records(payload: Any) -> list[DataflowRecord]:
    """Parse the (unwrapped) ``/api/tr/dataflows`` list into records."""
    if not isinstance(payload, list):
        raise ConnectorError(FORMAT_CHANGED, "veriportali dataflows response is not a list")
    records: list[DataflowRecord] = []
    for entry in payload:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        portal_id = str(entry["id"])
        code, parsed_version = split_portal_id(portal_id)
        footnotes = entry.get("footnotes")
        records.append(
            DataflowRecord(
                portal_id=portal_id,
                external_code=code,
                version=str(entry.get("version") or parsed_version or ""),
                name=str(entry.get("name") or code),
                description=entry.get("description"),
                period=entry.get("period"),
                updated_at=entry.get("updatedAt"),
                downloadable=bool(entry.get("downloadable")),
                category_path=_category_path(entry.get("category")),
                footnotes=[str(value) for value in footnotes]
                if isinstance(footnotes, list)
                else [],
            )
        )
    return records


def _content_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """A portal attribute object without the volatile ``seen_at`` stamp."""
    return {
        key: value for key, value in (attributes or {}).items() if key not in _VOLATILE_ATTRIBUTES
    }


def sync_veriportali_catalog(
    session: Session,
    records: Iterable[DataflowRecord],
    *,
    institution_id: int,
    complete: bool = True,
    now: datetime | None = None,
) -> VeriPortaliSync:
    """Merge the portal dataflow catalogue into the TÜİK datasets.

    A record already catalogued as a dataset only has
    ``attributes['veriportali']`` merged in; its dimensions and codes are never
    touched (calling :func:`upsert_dataset` with empty dimensions would mark every
    code removed). A record that is not catalogued yet creates a dimensionless
    dataset. A record that disappears from a COMPLETE list gets
    ``attributes['veriportali']['removed_at']`` (never deleted); a reappearing one
    clears it. Other datasets of the institution are left untouched.
    """
    now = now or datetime.now(UTC)
    seen_at = now.isoformat()
    grouped: dict[str, list[DataflowRecord]] = {}
    for record in records:
        grouped.setdefault(record.external_code, []).append(record)
    existing = list(
        session.scalars(sa.select(Dataset).where(Dataset.institution_id == institution_id))
    )
    existing_by_code = {dataset.external_code: dataset for dataset in existing}
    by_code: dict[str, DataflowRecord] = {}
    for code, candidates in grouped.items():
        catalogued = existing_by_code.get(code)
        version = (catalogued.attributes or {}).get("version") if catalogued else None
        chosen = select_dataflow(candidates, version=str(version) if version else None)
        if chosen is not None:
            by_code[code] = chosen
    created = updated = unchanged = 0
    for code, record in by_code.items():
        incoming = record.attributes(seen_at=seen_at)
        dataset = existing_by_code.get(code)
        if dataset is None:
            meta = DatasetMeta(
                external_code=code,
                name=record.name,
                description=record.description,
                source_category=" / ".join(record.category_path) or None,
                attributes={"channel": CHANNEL, VERIPORTALI_ATTRIBUTE: incoming},
                dimensions=[],
            )
            outcome, dataset = upsert_dataset(session, institution_id, meta)
            existing_by_code[code] = dataset
            if outcome == INSERTED:
                created += 1
            else:
                updated += 1
            continue
        attributes = dict(dataset.attributes or {})
        previous = attributes.get(VERIPORTALI_ATTRIBUTE)
        changed = _content_attributes(previous) != _content_attributes(incoming)
        attributes[VERIPORTALI_ATTRIBUTE] = incoming
        dataset.attributes = attributes
        if changed:
            updated += 1
        else:
            unchanged += 1

    removed = 0
    if complete:
        for code, dataset in existing_by_code.items():
            if code in by_code:
                continue
            attributes = dict(dataset.attributes or {})
            portal = attributes.get(VERIPORTALI_ATTRIBUTE)
            if not isinstance(portal, dict) or portal.get("removed_at"):
                continue
            portal = dict(portal)
            portal["removed_at"] = seen_at
            attributes[VERIPORTALI_ATTRIBUTE] = portal
            dataset.attributes = attributes
            removed += 1
    session.flush()
    return VeriPortaliSync(
        created=created, updated=updated, unchanged=unchanged, removed=removed, complete=complete
    )


@dataclass(frozen=True)
class VeriPortaliSync:
    """Outcome of one :func:`sync_veriportali_catalog` call."""

    created: int
    updated: int
    unchanged: int
    removed: int
    complete: bool


# --- press catalogue --------------------------------------------------------


@dataclass(frozen=True)
class PressType:
    """One current bulletin type from ``/api/tr/press`` (used only for subject)."""

    external_id: str
    title: str
    category_name: str


@dataclass(frozen=True)
class PressItem:
    """One catalogued press release (a ``documents`` row to be)."""

    external_id: str
    title: str
    subject: str | None
    doc_type: str
    year: int | None
    url: str
    published_at: date | None = None
    language: str = "tr"
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarCrawl:
    """Every release parsed from the requested years plus completeness facts."""

    items: list[PressItem]
    skipped_without_id: int = 0
    failed_years: list[int] = field(default_factory=list)
    covers_all_years: bool = True

    @property
    def complete(self) -> bool:
        """True only for a full year range where every year parsed and no row lost its id.

        A partial year range (e.g. ``--from-year 2025``) is never complete: marking
        removals from it would flag every release of the other years as removed.
        """
        return self.covers_all_years and not self.failed_years and self.skipped_without_id == 0


def covers_all_press_years(from_year: int, to_year: int, *, today: date | None = None) -> bool:
    """True when ``from_year..to_year`` spans every calendar year up to this year."""
    current = (today or date.today()).year
    return from_year <= PRESS_FIRST_YEAR and to_year >= current


_WHITESPACE = re.compile(r"\s+")


def _normalize_title(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def press_id_from_link(link: Any) -> str | None:
    """The press id: the digits after the LAST ``-`` of the ``p`` query parameter."""
    if not isinstance(link, str) or not link:
        return None
    values = parse_qs(urlparse(link).query).get("p") or []
    if not values:
        return None
    value = unquote(values[0])
    if "-" not in value:
        return None
    candidate = value.rsplit("-", 1)[-1].strip()
    return candidate if candidate.isdigit() else None


def _press_id_from_url(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    tail = urlparse(url).path.rstrip("/").split("/")[-1]
    return tail if tail.isdigit() else None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError:
        return None


def parse_press_types(payload: Any) -> list[PressType]:
    """Parse ``/api/tr/press`` current bulletin types."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "veriportali press list is not an object")
    types: list[PressType] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        types.append(
            PressType(
                external_id=str(entry.get("id") or ""),
                title=title,
                category_name=str(entry.get("categoryName") or ""),
            )
        )
    return types


def parse_calendar(
    payload: Any,
    *,
    base_url: str,
    press_types: Iterable[PressType] = (),
) -> tuple[list[PressItem], int]:
    """Parse one year's calendar into TÜİK ``PressItem`` rows and skipped count.

    Only ``yayindaOlanlarList`` (published) is catalogued; ``yayindaOlmayanlarList``
    (upcoming) is ignored. Non-TÜİK institutions are skipped; a TÜİK row without a
    parseable press id is counted and skipped (never guessed).
    """
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "veriportali calendar response is not an object")
    rows = payload.get("yayindaOlanlarList")
    if not isinstance(rows, list):
        raise ConnectorError(FORMAT_CHANGED, "veriportali calendar has no yayindaOlanlarList")
    categories = {
        _normalize_title(entry.title): entry.category_name for entry in press_types if entry.title
    }
    items: list[PressItem] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("sorumluKisaAd") or "") != "TÜİK":
            continue
        press_id = press_id_from_link(row.get("link"))
        if press_id is None:
            skipped += 1
            continue
        title = str(row.get("adi") or "").strip()
        raw_date = row.get("gTarih")
        published = _parse_date(raw_date)
        items.append(
            PressItem(
                external_id=press_id,
                title=title or press_id,
                subject=categories.get(_normalize_title(title)),
                doc_type=DOC_TYPE,
                year=published.year if published else None,
                published_at=published,
                url=f"{base_url.rstrip('/')}/tr/press/{press_id}",
                attributes={
                    "period": row.get("donemi"),
                    "published_at_raw": raw_date,
                    "unit": row.get("birimi"),
                    "calendar_link": row.get("link"),
                },
            )
        )
    return items, skipped


def load_press_catalog(
    session: Session,
    items: Iterable[PressItem],
    *,
    institution_id: int | None = None,
    source: str = SOURCE,
    complete: bool = True,
    now: datetime | None = None,
) -> DocumentLoad:
    """Idempotently upsert press documents (never delete); mark removals if complete.

    ``attributes['press']`` (written by an on-demand :func:`fetch_press_release`)
    survives the catalogue upsert; ``content_text`` is never touched by it.
    """
    return load_documents(
        session,
        items,
        institution_id=institution_id,
        source=source,
        complete=complete,
        now=now,
        preserved_attributes=(PRESS_ATTRIBUTE,),
    )


# --- press detail -----------------------------------------------------------


@dataclass(frozen=True)
class DatasetLink:
    """A statistical table's dataset reference (code + optional version)."""

    dataset_code: str
    dataset_version: str | None = None
    title: str | None = None
    relation: str = "statistical_table"


@dataclass(frozen=True)
class PressDetail:
    """One on-demand press release detail, normalised for storage."""

    external_id: str
    title: str
    period: str | None
    press_date: date | None
    published_at_raw: str | None
    content_text: str
    attributes: dict[str, Any]
    links: list[DatasetLink] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    """Collect text from HTML, turning block/``<br>`` tags into newlines."""

    _BLOCKS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "ul",
            "ol",
            "tr",
            "table",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "section",
            "article",
            "blockquote",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw: str) -> str:
    """Plain text from an HTML body: tags dropped, entities decoded, blank runs collapsed."""
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    lines = [
        line.strip() for line in parser.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    collapsed: list[str] = []
    for line in lines:
        if line:
            collapsed.append(line)
        elif collapsed and collapsed[-1] != "":
            collapsed.append("")
    return "\n".join(collapsed).strip()


def parse_statistical_table_url(url: Any) -> tuple[str, str | None] | None:
    """Parse a databrowser2 table URL fragment into ``(dataset_code, version)``."""
    if not isinstance(url, str) or not url:
        return None
    fragment = urlparse(url).fragment or ""
    tail = unquote(fragment.rstrip("/").split("/")[-1])
    parts = tail.split(",")
    if len(parts) != 3 or not parts[1]:
        return None
    return parts[1], (parts[2] or None)


def _absolute_url(url: Any, base_url: str) -> Any:
    if not isinstance(url, str) or not url:
        return url
    if url.startswith("http"):
        return url
    return f"{base_url.rstrip('/')}/{url.lstrip('/')}"


def _absolute_entries(entries: Any, base_url: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        result.append(
            {
                "type": entry.get("type"),
                "title": entry.get("title"),
                "url": _absolute_url(entry.get("url"), base_url),
            }
        )
    return result


def parse_press_detail(payload: Any, *, base_url: str) -> PressDetail:
    """Turn a ``/api/tr/press/{id}`` body into text, attributes and dataset links.

    ``metadatas`` is deliberately not kept (it stays only in the raw MinIO body).
    ``isError`` is a ``not_found``: unknown press ids answer 200 with that flag.
    """
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "veriportali press detail is not an object")
    if payload.get("isError"):
        raise ConnectorError(NOT_FOUND, str(payload.get("message") or "press release not found"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ConnectorError(FORMAT_CHANGED, "veriportali press detail has no data object")
    content = data.get("content")
    text = html_to_text(content if isinstance(content, str) else "")
    raw_date = data.get("date")
    published = _parse_date(raw_date)

    statistical_tables: list[dict[str, Any]] = []
    links: list[DatasetLink] = []
    for entry in data.get("statisticalTables") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        item: dict[str, Any] = {
            "type": entry.get("type"),
            "title": entry.get("title"),
            "url": url,
        }
        parsed = parse_statistical_table_url(url)
        if parsed is not None:
            code, version = parsed
            item["dataset_code"] = code
            item["version"] = version
            links.append(
                DatasetLink(dataset_code=code, dataset_version=version, title=entry.get("title"))
            )
        statistical_tables.append(item)

    previous: list[dict[str, Any]] = []
    for entry in data.get("previousPresses") or []:
        if not isinstance(entry, dict):
            continue
        previous.append(
            {
                "title": entry.get("title"),
                "period": entry.get("period"),
                "id": _press_id_from_url(entry.get("url")),
            }
        )

    attributes: dict[str, Any] = {
        "number": data.get("number"),
        "period": data.get("period"),
        "date": raw_date,
        "contact_email": data.get("contactEmail"),
        "tables": _absolute_entries(data.get("tables"), base_url),
        "reports": _absolute_entries(data.get("reports"), base_url),
        "statistical_tables": statistical_tables,
        "previous_presses": previous,
    }
    return PressDetail(
        external_id=str(data.get("id") or ""),
        title=str(data.get("title") or ""),
        period=data.get("period"),
        press_date=published,
        published_at_raw=raw_date,
        content_text=text,
        attributes=attributes,
        links=links,
    )


# --- HTTP client ------------------------------------------------------------


class VeriPortaliClient:
    """Retrying, throttle/WAF-aware client for the Veri Portalı JSON APIs."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        press_base_url: str | None = None,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = INSTITUTION,
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = (base_url or resolved.tuik_veriportali_base_url).rstrip("/")
        self._press_base_url = (press_base_url or resolved.tuik_veriportali_press_base_url).rstrip(
            "/"
        )
        self._user_agent = resolved.tuik_veriportali_user_agent
        self._timeout_s = resolved.tuik_veriportali_request_timeout_s
        self._max_concurrency = resolved.tuik_veriportali_max_concurrency
        self._max_retries = resolved.tuik_veriportali_max_retries
        self._retry_backoff_s = resolved.tuik_veriportali_retry_backoff_s
        self._throttle_wait_s = resolved.tuik_veriportali_throttle_wait_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        self._throttled = False
        self._dataflows: list[DataflowRecord] | None = None
        self._dataflows_by_code: dict[str, list[DataflowRecord]] = {}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def press_base_url(self) -> str:
        return self._press_base_url

    @property
    def throttled(self) -> bool:
        return self._throttled

    @property
    def max_concurrency(self) -> int:
        return 1 if self._throttled else self._max_concurrency

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- plumbing ---------------------------------------------------------

    def _headers(self, *, xhr: bool) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent, "Accept": JSON_ACCEPT}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    def _store_raw(self, dataset: str, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(self._institution, dataset, channel, payload, ext, store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning(
                "veriportali raw store failed for %s/%s", dataset, channel, exc_info=True
            )
            return None

    @staticmethod
    def _error_message(content: bytes) -> str:
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return content[:200].decode("utf-8", errors="replace")
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or payload)[:300]
        return str(payload)[:300]

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    @staticmethod
    def _is_text_body(content_type: str | None) -> bool:
        return "text/plain" in (content_type or "").lower()

    def _request(
        self,
        method: str,
        url: str,
        *,
        dataset: str,
        channel: str,
        error_ext: str = "json",
        xhr: bool = True,
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._client.request(method, url, headers=self._headers(xhr=xhr))
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"veriportali timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("veriportali timeout on %s (attempt %d)", channel, attempt + 1)
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"veriportali transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning(
                    "veriportali transport error on %s (attempt %d)", channel, attempt + 1
                )
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            status = response.status_code
            content_type = response.headers.get("content-type")
            if status in (200, 206):
                if is_throttle_response(content_type, response.content):
                    key = self._store_raw(dataset, f"{channel}-throttle", response.content, "html")
                    self._throttled = True
                    if attempt >= self._max_retries:
                        raise ConnectorError(
                            THROTTLED,
                            "veriportali returned a throttle (Yönlendiriliyor) page",
                            raw_object_key=key,
                        )
                    wait = self._throttle_wait_s * (2**attempt)
                    logger.warning(
                        "veriportali throttle page on %s; waiting %.1fs (attempt %d)",
                        channel,
                        wait,
                        attempt + 1,
                    )
                    self._sleeper(wait)
                    attempt += 1
                    continue
                return response

            if status == 403:
                key = self._store_raw(dataset, f"{channel}-error", response.content, "txt")
                raise ConnectorError(
                    SOURCE_ERROR,
                    "veriportali WAF blocked (check headers)",
                    raw_object_key=key,
                )
            if status == 404:
                key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
                if self._is_text_body(content_type):
                    raise ConnectorError(
                        FORMAT_CHANGED,
                        "veriportali returned HTTP 404 text/plain; the path exists but the "
                        "request needs browser headers (X-Requested-With)",
                        raw_object_key=key,
                    )
                raise ConnectorError(
                    NOT_FOUND,
                    f"HTTP 404: {self._error_message(response.content)}",
                    raw_object_key=key,
                )
            if status >= 500:
                if attempt >= self._max_retries:
                    key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"HTTP {status}: {self._error_message(response.content)}",
                        raw_object_key=key,
                    )
                logger.warning(
                    "veriportali HTTP %d on %s (attempt %d)", status, channel, attempt + 1
                )
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            key = self._store_raw(dataset, f"{channel}-error", response.content, error_ext)
            raise ConnectorError(
                SOURCE_ERROR,
                f"HTTP {status}: {self._error_message(response.content)}",
                raw_object_key=key,
            )

    # --- catalogue --------------------------------------------------------

    def dataflows(self) -> list[DataflowRecord]:
        """The whole dataflow catalogue (cached; 510 records measured live)."""
        if self._dataflows is None:
            url = f"{self._base_url}{DATAFLOWS_PATH}"
            response = self._request("GET", url, dataset="dataflows", channel=CATALOG_CHANNEL)
            key = self._store_raw("dataflows", CATALOG_CHANNEL, response.content, "json")
            payload = parse_json(response.content, raw_object_key=key)
            self._dataflows = parse_dataflow_records(payload)
            grouped: dict[str, list[DataflowRecord]] = {}
            for record in self._dataflows:
                grouped.setdefault(record.external_code, []).append(record)
            self._dataflows_by_code = grouped
            logger.info("veriportali catalog: %d dataflows", len(self._dataflows))
        return self._dataflows

    def find_dataflow(
        self, dataset_code: str, *, version: str | None = None
    ) -> DataflowRecord | None:
        """The portal record of a code; ``version`` picks among several versions."""
        self.dataflows()
        return select_dataflow(self._dataflows_by_code.get(dataset_code, []), version=version)

    def is_downloadable(self, dataset_code: str, *, version: str | None = None) -> bool:
        record = self.find_dataflow(dataset_code, version=version)
        return bool(record and record.downloadable)

    def dataflow_file(self, portal_id: str, fmt: str = "json") -> RawResponse:
        """Download a whole dataflow as ``json``/``csv``/``xml`` (``+`` encoded)."""
        safe = quote(portal_id, safe="")
        url = f"{self._base_url}{DATAFLOWS_PATH}/{safe}/file/{fmt}"
        dataset = portal_id.replace("+", "_")
        response = self._request("GET", url, dataset=dataset, channel=FILE_CHANNEL)
        key = self._store_raw(dataset, f"{FILE_CHANNEL}-{fmt}", response.content, fmt)
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    # --- press ------------------------------------------------------------

    def press_list(self) -> RawResponse:
        url = f"{self._base_url}{PRESS_LIST_PATH}"
        response = self._request("GET", url, dataset="press-list", channel=PRESS_LIST_CHANNEL)
        key = self._store_raw("press-list", PRESS_LIST_CHANNEL, response.content, "json")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def press_detail(self, press_id: str) -> RawResponse:
        safe = quote(str(press_id), safe="")
        url = f"{self._base_url}{PRESS_LIST_PATH}/{safe}"
        response = self._request("GET", url, dataset=f"press-{safe}", channel=PRESS_CHANNEL)
        key = self._store_raw(f"press-{safe}", PRESS_CHANNEL, response.content, "json")
        raw = RawResponse(response.status_code, response.content, dict(response.headers), key)
        payload = raw.json()
        if isinstance(payload, dict) and payload.get("isError"):
            raise ConnectorError(
                NOT_FOUND,
                str(payload.get("message") or "press release not found"),
                raw_object_key=key,
            )
        return raw

    def calendar(self, year: int) -> RawResponse:
        url = f"{self._press_base_url}{CALENDAR_PATH}?yil={int(year)}"
        response = self._request(
            "GET",
            url,
            dataset=f"calendar-{int(year)}",
            channel=f"{CALENDAR_CHANNEL}-{int(year)}",
            xhr=False,
        )
        key = self._store_raw(
            f"calendar-{int(year)}", f"{CALENDAR_CHANNEL}-{int(year)}", response.content, "json"
        )
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    # --- backup series fetch ---------------------------------------------

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str],
        start: date = date(2000, 1, 1),
        version: str | None = None,
    ) -> FetchResult:
        """Fetch one series from the whole-dataset JSON and select it by key."""
        record = self.find_dataflow(dataset_code, version=version)
        if record is None or not record.downloadable:
            raise ConnectorError(
                NOT_FOUND,
                f"veriportali has no downloadable dataflow {dataset_code!r}",
            )
        response = self.dataflow_file(record.portal_id, "json")
        points = points_from_sdmx_json(response.json(), codes)
        points = [point for point in points if point[0] >= start]
        if not points:
            raise ConnectorError(
                EMPTY,
                f"{dataset_code}: veriportali returned no observations",
                raw_object_key=response.raw_object_key,
            )
        external_code = f"{dataset_code}:" + ".".join(codes[dim] for dim in order)
        keys = [key for key in (response.raw_object_key,) if key]
        return FetchResult(
            external_code=external_code,
            points=points,
            raw_object_keys=keys,
            channel=CHANNEL,
        )


# --- on-demand detail loader ------------------------------------------------


def _resolve_dataset_id(
    session: Session, institution_id: int | None, dataset_code: str
) -> int | None:
    if institution_id is None:
        return None
    return session.scalar(
        sa.select(Dataset.id).where(
            Dataset.institution_id == institution_id,
            Dataset.external_code == dataset_code,
        )
    )


def _upsert_link(
    session: Session,
    document_id: int,
    link: DatasetLink,
    dataset_id: int | None,
) -> None:
    """Insert or refresh one ``(document_id, dataset_code)`` link (never deletes)."""
    row = session.scalar(
        sa.select(DocumentDatasetLink).where(
            DocumentDatasetLink.document_id == document_id,
            DocumentDatasetLink.dataset_code == link.dataset_code,
        )
    )
    if row is None:
        session.add(
            DocumentDatasetLink(
                document_id=document_id,
                dataset_code=link.dataset_code,
                dataset_version=link.dataset_version,
                dataset_id=dataset_id,
                relation=link.relation,
                title=link.title,
            )
        )
        return
    if link.dataset_version and row.dataset_version != link.dataset_version:
        row.dataset_version = link.dataset_version
    # Re-resolve a link that was NULL when the dataset was not catalogued yet.
    if dataset_id is not None:
        row.dataset_id = dataset_id
    if link.title and row.title != link.title:
        row.title = link.title
    row.relation = link.relation


def fetch_press_release(
    session: Session,
    client: VeriPortaliClient,
    press_id: str,
    *,
    institution_id: int | None = None,
    base_url: str | None = None,
    now: datetime | None = None,
) -> PressFetchResult:
    """Fetch one press release: raw JSON in MinIO, text, attributes and links.

    If the id is not catalogued yet the ``documents`` row is created from the
    detail. ``content_text`` and ``attributes['press']`` are always rewritten;
    the bulletin -> dataset links are upserted (idempotent, re-resolving a NULL
    ``dataset_id``). Excel/PDF files are never downloaded.
    """
    now = now or datetime.now(UTC)
    resolved_base = (base_url or client.base_url).rstrip("/")
    response = client.press_detail(str(press_id))
    detail = parse_press_detail(response.json(), base_url=resolved_base)
    document = session.scalar(
        sa.select(Document).where(Document.source == SOURCE, Document.external_id == str(press_id))
    )
    created = document is None
    if document is None:
        published = detail.press_date
        document = Document(
            institution_id=institution_id,
            source=SOURCE,
            external_id=str(press_id),
            doc_type=DOC_TYPE,
            title=detail.title or str(press_id),
            subject=None,
            year=published.year if published else None,
            published_at=published,
            url=f"{resolved_base}/tr/press/{press_id}",
            language="tr",
            attributes={},
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(document)
        session.flush()
    press_attributes = dict(detail.attributes)
    press_attributes["raw_object_key"] = response.raw_object_key
    press_attributes["fetched_at"] = now.isoformat()
    attributes = dict(document.attributes or {})
    attributes[PRESS_ATTRIBUTE] = press_attributes
    document.attributes = attributes
    document.content_text = detail.content_text
    session.flush()

    resolved = 0
    for link in detail.links:
        dataset_id = _resolve_dataset_id(session, institution_id, link.dataset_code)
        if dataset_id is not None:
            resolved += 1
        _upsert_link(session, document.id, link, dataset_id)
    session.flush()
    return PressFetchResult(
        document_id=document.id,
        created=created,
        content_length=len(detail.content_text),
        links_written=len(detail.links),
        links_resolved=resolved,
    )


@dataclass(frozen=True)
class PressFetchResult:
    """Outcome of one :func:`fetch_press_release` call."""

    document_id: int
    created: bool
    content_length: int
    links_written: int
    links_resolved: int


__all__ = [
    "select_dataflow",
    "PRESS_FIRST_YEAR",
    "covers_all_press_years",
    "CATALOG_CHANNEL",
    "CHANNEL",
    "CalendarCrawl",
    "DOC_TYPE",
    "DataflowRecord",
    "DatasetLink",
    "FILE_CHANNEL",
    "INSTITUTION",
    "PRESS_CHANNEL",
    "PressDetail",
    "PressFetchResult",
    "PressItem",
    "PressType",
    "RawResponse",
    "SOURCE",
    "VERIPORTALI_ATTRIBUTE",
    "VeriPortaliClient",
    "VeriPortaliSync",
    "fetch_press_release",
    "html_to_text",
    "load_press_catalog",
    "parse_calendar",
    "parse_dataflow_records",
    "parse_json",
    "parse_press_detail",
    "parse_press_types",
    "parse_statistical_table_url",
    "press_id_from_link",
    "split_portal_id",
    "sync_veriportali_catalog",
]
