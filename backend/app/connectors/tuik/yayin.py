"""TÜİK Biruni publication system (``biruni.tuik.gov.tr/yayin``) connector.

The publication system is a ZK 7 AU (asynchronous update) application, not a
JSON API. One bootstrap GET of the index page returns the first page's records
inside the HTML plus the session cookies, the desktop id and the pager; each
following page is a POST to ``/yayin/zkau`` carrying the paging component's
uuid and a zero-based page index. Both bodies are ZK client-server brackets, so
the parser reads the ``zul.sel.Listitem`` blocks and their quoted properties
(``label``, ``href``) rather than treating the payload as JSON.

Live measurements (2026-10-01): 634 records, 9 per page, 71 pages; page 2
starts at ``yayin_no`` 718, 717, 716 … The front protection silently stalls
requests that arrive too fast, so :class:`YayinClient` spaces requests at least
``settings.tuik_yayin_pause_s`` apart (the first request is immediate).

Loaded rows are source-independent (``source='tuik_yayin'``) and idempotent. A
document is identified by ``(source, external_id)``; one that disappears gets
``attributes['removed_at']`` (never deleted) and a reappearing row clears it.
Removals are only marked after a COMPLETE crawl: a partial crawl (a fetch that
failed, or a page budget) must never mark anything removed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    FORMAT_CHANGED,
    SOURCE_ERROR,
    TIMEOUT,
    ConnectorError,
    ObjectStore,
    store_raw,
)
from app.connectors.tuik.client import RawResponse
from app.data.models import Document

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://biruni.tuik.gov.tr"
INSTITUTION = "tuik"
SOURCE = "tuik_yayin"
DATASET = "yayin"

INDEX_PATH = "/yayin/views/visitorPages/index.zul"
AU_PATH = "/yayin/zkau"
INDEX_URL = f"{DEFAULT_BASE_URL}{INDEX_PATH}"
INDEX_CHANNEL = "yayin-index"
PAGE_CHANNEL = "yayin-page"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Bulk-insert batch size; documents are few, but the loader stays batch-shaped.
_BATCH = 1000

# The listbox that holds the catalogue rows; the correct pager is the first
# Paging component AFTER it (other Paging components live on the page too).
_LISTBOX_MARKER = "id:'listYayinlar'"
_LISTITEM_MARKER = "['zul.sel.Listitem'"
_PAGING_MARKER = "['zul.mesh.Paging'"

_DESKTOP_ID = re.compile(r"\{dt:'([^']+)'")
_YAYIN_NO = re.compile(r"[?&]yayin_no=([^&]+)")
_JSESSIONID = re.compile(r";jsessionid=[^?/#]+", re.IGNORECASE)
_YEAR = re.compile(r"(?:19|20)\d{2}")
_SOURCE_INDEX = re.compile(r"_index:(\d+)")
# The bootstrap page embeds the servlet session in links (`;jsessionid=...`);
# it changes every crawl, so it is stripped before anything is stored.
_JSESSIONID = re.compile(r";jsessionid=[^?#]*")
_NUMBER = "{}:(\\d+)"


@dataclass(frozen=True)
class YayinPager:
    """The catalogue pager taken from the listbox's Paging component."""

    uuid: str
    total_size: int
    page_size: int
    page_count: int


@dataclass(frozen=True)
class YayinItem:
    """One catalogue row: metadata plus the publication link."""

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
class YayinCrawl:
    """Every item fetched plus the pager facts needed for a completeness check."""

    total_size: int
    page_size: int
    page_count: int
    pages: int
    items: list[YayinItem]
    raw_object_keys: list[str] = field(default_factory=list)
    stalls: int = 0
    retries: int = 0

    @property
    def complete(self) -> bool:
        """True only when every page the pager promised was fetched and parsed."""
        return self.pages >= self.page_count and len(self.items) == self.total_size


@dataclass(frozen=True)
class DocumentLoad:
    """Outcome of one :func:`load_documents` call."""

    inserted: int
    updated: int
    unchanged: int
    removed: int


def _decode_zk(raw: str) -> str:
    """Decode the ZK string escapes (``\\xNN``/``\\uNNNN``/``\\n``/``\\'``)."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token[0] == "x":
            return chr(int(token[1:], 16))
        if token[0] == "u":
            return chr(int(token[1:], 16))
        return {"n": "\n", "r": "\r", "t": "\t"}.get(token, token)

    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", replace, raw)


def _balanced_block(text: str, start: int) -> str:
    """Return the ``[...]`` block starting at ``start`` (quote-aware)."""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ConnectorError(FORMAT_CHANGED, f"unclosed ZK block at offset {start}")


def _bracket_blocks(text: str, marker: str) -> list[str]:
    """Every balanced block that begins with ``marker`` (e.g. a Listitem)."""
    blocks: list[str] = []
    cursor = 0
    while True:
        start = text.find(marker, cursor)
        if start < 0:
            return blocks
        block = _balanced_block(text, start)
        blocks.append(block)
        cursor = start + len(block)


def _quoted_values(block: str, property_name: str) -> list[str]:
    """Every decoded ``property:'…'`` value in one ZK block."""
    marker = f"{property_name}:'"
    values: list[str] = []
    cursor = 0
    while True:
        start = block.find(marker, cursor)
        if start < 0:
            return values
        index = start + len(marker)
        raw = ""
        closed = False
        while index < len(block):
            char = block[index]
            if char == "\\" and index + 1 < len(block):
                raw += char + block[index + 1]
                index += 1
            elif char == "'":
                closed = True
                index += 1
                break
            else:
                raw += char
            index += 1
        if not closed:
            raise ConnectorError(FORMAT_CHANGED, f"unclosed ZK {property_name!r} value in a block")
        values.append(_decode_zk(raw))
        cursor = index


def _number(block: str, property_name: str) -> int | None:
    match = re.search(_NUMBER.format(property_name), block)
    return int(match.group(1)) if match else None


def parse_desktop_id(text: str) -> str:
    """The ZK desktop id (``{dt:'…'}``); required by every AU request."""
    match = _DESKTOP_ID.search(text)
    if not match:
        raise ConnectorError(FORMAT_CHANGED, "bootstrap page has no ZK desktop id")
    return match.group(1)


def parse_pager(text: str) -> YayinPager:
    """The catalogue pager: the first Paging component after ``listYayinlar``.

    Other Paging components exist earlier on the page and must not be picked;
    only the one carrying ``totalSize`` after the catalogue listbox is correct.
    """
    listbox = text.find(_LISTBOX_MARKER)
    if listbox < 0:
        raise ConnectorError(FORMAT_CHANGED, "response has no listYayinlar listbox")
    cursor = listbox
    while True:
        start = text.find(_PAGING_MARKER, cursor)
        if start < 0:
            raise ConnectorError(FORMAT_CHANGED, "no catalogue pager after listYayinlar")
        block = _balanced_block(text, start)
        total_size = _number(block, "totalSize")
        if total_size is not None:
            uuid_match = re.search(r"\['zul\.mesh\.Paging','([^']+)'", block)
            if not uuid_match:
                raise ConnectorError(FORMAT_CHANGED, "catalogue pager has no uuid")
            return YayinPager(
                uuid=uuid_match.group(1),
                total_size=total_size,
                page_size=_number(block, "pageSize") or 0,
                page_count=_number(block, "pageCount") or 0,
            )
        cursor = start + len(block)


def _absolute_url(href: str) -> str:
    url = href if href.startswith("http") else f"{DEFAULT_BASE_URL}{href}"
    return _JSESSIONID.sub("", url)


def _parse_year(title: str) -> int | None:
    """The last 4-digit year token in the title, or ``None`` when there is none.

    Never invented: the year comes from the source's title label only.
    """
    years = _YEAR.findall(title)
    return int(years[-1]) if years else None


def parse_items(text: str, *, page: int = 1) -> list[YayinItem]:
    """Parse the ``zul.sel.Listitem`` rows of one bootstrap/AU response.

    ``labels`` are the source's labels in order: subject, title, type (and any
    extra label is kept verbatim in ``attributes['labels']``). A row without a
    ``yayin_no`` link cannot be identified and is skipped; a row with no labels
    at all is a format change.
    """
    items: list[YayinItem] = []
    for block in _bracket_blocks(text, _LISTITEM_MARKER):
        hrefs = _quoted_values(block, "href")
        href = next((value for value in hrefs if "yayin_no=" in value), None)
        if href is None:
            continue
        external_match = _YAYIN_NO.search(href)
        if external_match is None:
            continue
        labels = _quoted_values(block, "label")
        if not labels:
            raise ConnectorError(FORMAT_CHANGED, "a catalogue row has no label")
        subject = labels[0] if len(labels) >= 2 else None
        title = labels[1] if len(labels) >= 2 else labels[0]
        doc_type = labels[2] if len(labels) >= 3 else ""
        href = _JSESSIONID.sub("", href)
        attributes: dict[str, Any] = {"labels": labels, "href": href, "page": page}
        index_match = _SOURCE_INDEX.search(block)
        if index_match:
            attributes["absolute_index"] = int(index_match.group(1)) + 1
        items.append(
            YayinItem(
                external_id=external_match.group(1),
                title=title,
                subject=subject,
                doc_type=doc_type,
                year=_parse_year(title),
                url=_absolute_url(href),
                attributes=attributes,
            )
        )
    return items


class YayinClient:
    """Retrying, raw-storing, request-spacing HTTP client for the ZK app."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_client: httpx.Client | None = None,
        store: ObjectStore | None = None,
        institution: str = INSTITUTION,
        settings_obj: Settings | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = (base_url or resolved.tuik_yayin_base_url).rstrip("/")
        self._timeout_s = resolved.tuik_yayin_request_timeout_s
        self._max_retries = resolved.tuik_yayin_max_retries
        self._retry_backoff_s = resolved.tuik_yayin_retry_backoff_s
        self._pause_s = resolved.tuik_yayin_pause_s
        self._stall_s = resolved.tuik_yayin_stall_s
        self._store = store
        self._institution = institution
        self._sleeper = sleeper
        self._clock = clock
        self._owns_client = http_client is None
        # One client for the whole crawl so the session cookies persist.
        self._client = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        self._last_request_at: float | None = None
        # ZK's AU protocol is sequence-numbered: the browser client seeds ZK-SID
        # from the bootstrap response and increments it after every AU response.
        # Reusing one value makes the server answer the previous page again.
        self._zk_sid: str | None = None
        self.retries = 0
        self.stalls = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _store_raw(self, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(self._institution, DATASET, channel, payload, ext, store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("yayin raw store failed for %s", channel, exc_info=True)
            return None

    @staticmethod
    def _error_message(content: bytes) -> str:
        return content[:300].decode("utf-8", errors="replace")

    def _sleep_backoff(self, attempt: int) -> None:
        self._sleeper(self._retry_backoff_s * (2**attempt))

    def _pause(self) -> None:
        """Space requests at least ``pause_s`` apart (the first one is immediate)."""
        if self._last_request_at is None:
            self._last_request_at = self._clock()
            return
        elapsed = self._clock() - self._last_request_at
        wait = self._pause_s - elapsed
        if wait > 0:
            self._sleeper(wait)
        self._last_request_at = self._clock()

    def _request(
        self,
        method: str,
        path: str,
        *,
        channel: str,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            self._pause()
            started = self._clock()
            try:
                response = self._client.request(method, url, data=data, headers=headers)
            except httpx.TimeoutException as exc:
                self.stalls += 1
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"yayin {path} timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                logger.warning("yayin timeout on %s (attempt %d)", path, attempt + 1)
                self.retries += 1
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"yayin transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                logger.warning("yayin transport error on %s (attempt %d)", path, attempt + 1)
                self.retries += 1
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            if self._clock() - started >= self._stall_s:
                self.stalls += 1
                logger.warning("yayin slow response on %s (possible silent throttle)", path)

            if response.status_code in (200, 206):
                return response
            if response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"HTTP {response.status_code}: {self._error_message(response.content)}",
                    )
                logger.warning(
                    "yayin HTTP %d on %s (attempt %d)", response.status_code, path, attempt + 1
                )
                self.retries += 1
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            raise ConnectorError(
                SOURCE_ERROR,
                f"HTTP {response.status_code}: {self._error_message(response.content)}",
            )

    def bootstrap(self) -> RawResponse:
        """GET the index page: first page's rows, cookies, desktop id and pager."""
        response = self._request(
            "GET",
            INDEX_PATH,
            channel=INDEX_CHANNEL,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        key = self._store_raw(INDEX_CHANNEL, response.content, "html")
        self._zk_sid = response.headers.get("zk-sid") or "1"
        return RawResponse(response.status_code, response.content, dict(response.headers), key)

    def _advance_zk_sid(self, response: httpx.Response) -> None:
        current = response.headers.get("zk-sid") or self._zk_sid or "1"
        try:
            value = int(current)
        except ValueError:
            value = 1
        self._zk_sid = str((value % 9999) + 1)

    def page(self, page: int, *, desktop_id: str, pager_uuid: str) -> RawResponse:
        """Fetch one 1-based page through the ZK AU paging command."""
        body = {
            "dtid": desktop_id,
            "cmd_0": "onPaging",
            "uuid_0": pager_uuid,
            "data_0": f'{{"":{page - 1}}}',
        }
        response = self._request(
            "POST",
            AU_PATH,
            channel=f"{PAGE_CHANNEL}-{page:03d}",
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": self._base_url,
                "Referer": f"{self._base_url}{INDEX_PATH}",
                "ZK-SID": self._zk_sid or "1",
            },
        )
        self._advance_zk_sid(response)
        key = self._store_raw(f"{PAGE_CHANNEL}-{page:03d}", response.content, "txt")
        return RawResponse(response.status_code, response.content, dict(response.headers), key)


def crawl_yayin(client: YayinClient, *, max_pages: int | None = None) -> YayinCrawl:
    """Fetch the bootstrap page and every following page, parsing the rows.

    ``max_pages`` caps the number of pages fetched (a partial crawl); the
    returned :attr:`YayinCrawl.complete` is then False and the caller must not
    mark removals.
    """
    raw_keys: list[str] = []
    bootstrap = client.bootstrap()
    if bootstrap.raw_object_key:
        raw_keys.append(bootstrap.raw_object_key)
    text = bootstrap.content.decode("utf-8", errors="replace")
    desktop_id = parse_desktop_id(text)
    pager = parse_pager(text)
    items = parse_items(text, page=1)

    pages = pager.page_count or 1
    if max_pages is not None:
        pages = min(pages, max(1, max_pages))
    for page in range(2, pages + 1):
        response = client.page(page, desktop_id=desktop_id, pager_uuid=pager.uuid)
        if response.raw_object_key:
            raw_keys.append(response.raw_object_key)
        items.extend(parse_items(response.content.decode("utf-8", errors="replace"), page=page))

    return YayinCrawl(
        total_size=pager.total_size,
        page_size=pager.page_size,
        page_count=pager.page_count,
        pages=pages,
        items=items,
        raw_object_keys=raw_keys,
        stalls=client.stalls,
        retries=client.retries,
    )


# Keys that describe where a row sat in the listing, not what it says. The
# source reorders rows between crawls (found live 2026-10-01: a re-run with no
# new publications reported 9 "updated" rows), so they must not count as change.
_VOLATILE_ATTRIBUTES = frozenset({"removed_at", "page", "absolute_index"})


def _content_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """The stored attributes without the removal marker and listing position."""
    return {
        key: value for key, value in (attributes or {}).items() if key not in _VOLATILE_ATTRIBUTES
    }


def _plan_document_changes(
    incoming: Sequence[YayinItem],
    existing: Sequence[Document],
    *,
    institution_id: int | None,
    source: str,
    now: datetime,
    complete: bool,
    preserved_attributes: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], int, int, int, list[Document]]:
    """Decide which documents to upsert and which existing rows to mark removed.

    Pure: returns ``(upsert_rows, inserted, updated, unchanged, removed_rows)``.
    An incoming item is keyed by ``external_id``. ``last_seen_at`` is bumped for
    every seen row, so an outcome of ``unchanged`` means the content did not
    change. Removals are only proposed when ``complete`` is True, so a partial
    crawl never marks anything removed. ``preserved_attributes`` names keys written
    by another path (e.g. an on-demand detail fetch) that survive a catalogue
    upsert when the incoming item does not carry them.
    """
    existing_by_id = {row.external_id: row for row in existing}
    incoming_by_id = {item.external_id: item for item in incoming}
    inserted = updated = unchanged = 0
    upsert_rows: list[dict[str, Any]] = []
    for external_id, item in incoming_by_id.items():
        attributes = _content_attributes(item.attributes)
        previous = existing_by_id.get(external_id)
        if previous is not None:
            for key in preserved_attributes:
                if key not in attributes and key in (previous.attributes or {}):
                    attributes[key] = previous.attributes[key]
        if previous is None:
            inserted += 1
        else:
            had_removed = "removed_at" in (previous.attributes or {})
            content_changed = (
                previous.doc_type != item.doc_type
                or previous.title != item.title
                or previous.subject != item.subject
                or previous.year != item.year
                or previous.published_at != item.published_at
                or previous.url != item.url
                or previous.language != item.language
                or previous.institution_id != institution_id
                or _content_attributes(previous.attributes) != attributes
            )
            if content_changed or had_removed:
                updated += 1
            else:
                unchanged += 1
        upsert_rows.append(
            {
                "institution_id": institution_id,
                "source": source,
                "external_id": external_id,
                "doc_type": item.doc_type,
                "title": item.title,
                "subject": item.subject,
                "year": item.year,
                "published_at": item.published_at,
                "url": item.url,
                "language": item.language,
                "attributes": attributes,
                "first_seen_at": previous.first_seen_at if previous is not None else now,
                "last_seen_at": now,
            }
        )
    removed_rows: list[Document] = []
    if complete:
        removed_rows = [
            row
            for external_id, row in existing_by_id.items()
            if external_id not in incoming_by_id and "removed_at" not in (row.attributes or {})
        ]
    return upsert_rows, inserted, updated, unchanged, removed_rows


def load_documents(
    session: Session,
    items: Iterable[YayinItem],
    *,
    institution_id: int | None = None,
    source: str = SOURCE,
    complete: bool = True,
    now: datetime | None = None,
    preserved_attributes: Sequence[str] = (),
) -> DocumentLoad:
    """Idempotently upsert documents (never delete); mark removals when complete."""
    now = now or datetime.now(UTC)
    existing = session.scalars(sa.select(Document).where(Document.source == source)).all()
    upsert_rows, inserted, updated, unchanged, removed_rows = _plan_document_changes(
        list(items),
        list(existing),
        institution_id=institution_id,
        source=source,
        now=now,
        complete=complete,
        preserved_attributes=preserved_attributes,
    )
    for start in range(0, len(upsert_rows), _BATCH):
        batch = upsert_rows[start : start + _BATCH]
        statement = pg_insert(Document).values(batch)
        statement = statement.on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_={
                "institution_id": statement.excluded.institution_id,
                "doc_type": statement.excluded.doc_type,
                "title": statement.excluded.title,
                "subject": statement.excluded.subject,
                "year": statement.excluded.year,
                "published_at": statement.excluded.published_at,
                "url": statement.excluded.url,
                "language": statement.excluded.language,
                "attributes": statement.excluded.attributes,
                "last_seen_at": statement.excluded.last_seen_at,
            },
        )
        session.execute(statement)

    if removed_rows:
        table = Document.__table__
        session.execute(
            sa.update(table)
            .where(table.c.id == sa.bindparam("document_id"))
            .values(attributes=sa.bindparam("attrs")),
            [
                {
                    "document_id": row.id,
                    "attrs": {**(row.attributes or {}), "removed_at": now.isoformat()},
                }
                for row in removed_rows
            ],
        )
    session.flush()
    return DocumentLoad(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        removed=len(removed_rows),
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DocumentLoad",
    "INDEX_CHANNEL",
    "INSTITUTION",
    "PAGE_CHANNEL",
    "SOURCE",
    "USER_AGENT",
    "YayinClient",
    "YayinCrawl",
    "YayinItem",
    "YayinPager",
    "crawl_yayin",
    "load_documents",
    "parse_desktop_id",
    "parse_items",
    "parse_pager",
]
