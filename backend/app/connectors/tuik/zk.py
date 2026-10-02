"""Reusable client for the legacy TÜİK ZK "DHTML" AU applications.

Several Biruni applications (``turizmapp`` today, the election system next) are
ZK 5/6 web apps that speak the *old* AU protocol over plain HTTP:

1. ``GET <page>`` returns the whole component tree (hidden components included)
   plus the session cookies, the desktop id and the servlet session.
2. Every interaction is a ``POST <base>/zkau;jsessionid=<id>`` with a flat
   ``dtid`` / ``cmd.N`` / ``uuid.N`` / ``data.N`` body. Radios and checkboxes
   answer ``onCheck``, listboxes ``onSelect`` (one ``data.0`` per selected item)
   and buttons ``onClick`` with three mouse-coordinate strings.
3. Responses are single-line XML with ``<r><c>outer</c>…`` listbox bodies
   (header + items), ``<r><c>setAttr</c>…visibility…`` toggles, a
   ``<r><c>redirect</c>`` for generated reports and ``HATALI`` modal text for
   validation failures.

The front silently stalls requests that arrive too fast, so requests are spaced
at least :attr:`LegacyZkClient.pause_s` apart (>= 2.0 s by default; the first
request is immediate). A session that dies is re-bootstrapped by the caller
(:meth:`LegacyZkClient.bootstrap`); the turizm walker restarts its whole form.

Reports are legacy Excel-HTML with ``colspan``/``rowspan`` and spacer cells;
:func:`parse_report_grid` resolves the spans into a dense grid and drops the
all-empty rows/columns, and :func:`parse_turkish_number` reads the Turkish
number format (``.`` thousands, ``,`` decimals).
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx

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

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://biruni.tuik.gov.tr"
DEFAULT_REPORT_ENCODING = "windows-1254"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_JSESSIONID = re.compile(r";?jsessionid=([A-Za-z0-9_-]+)")
_DESKTOP_ID = re.compile(r"dtid['\"]?\s*[:=]\s*['\"]([^'\"]+)")
_CHARSET = re.compile(r"charset=[\"']?([\w-]+)", re.IGNORECASE)

_RADIO_SPAN = re.compile(
    r'<span id="([^"]+)" z\.type="zul\.widget\.Radio"[^>]*>(.*?)</span>', re.DOTALL
)
_CKBOX_SPAN = re.compile(
    r'<span id="([^"]+)" z\.type="zul\.widget\.Ckbox"[^>]*>(.*?)</span>', re.DOTALL
)
_BUTTON = re.compile(
    r'<button[^>]*\bid="([^"]+)"[^>]*z\.type="zul\.widget\.Button"[^>]*>(.*?)</button>',
    re.DOTALL,
)
# ZK renders a button as either ``<button>label</button>`` or
# ``<input type="button" value="label">`` depending on the browser it thinks it
# is talking to; both shapes appear live.
_BUTTON_INPUT = re.compile(
    r'<input[^>]*\bid="([^"]+)"[^>]*z\.type="zul\.widget\.Button"[^>]*\bvalue="([^"]*)"',
    re.DOTALL,
)
_LIBOX = re.compile(r'<div id="([^"]+)" z\.type="zul\.sel\.Libox"[^>]*>')
_INPUT_NAME = re.compile(r'<input[^>]*\bname="([^"]+)"')
_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

_OUTER = re.compile(
    r"<r><c>outer</c>\s*<d>([^<]+)</d>\s*<d><!\[CDATA\[(.*?)\]\]></d>\s*</r>", re.DOTALL
)
_HEADER = re.compile(r'z\.type="Lhr"[^>]*>(.*?)</th>', re.DOTALL)
_ITEM = re.compile(r'<tr id="([^"]+)" z\.type="Lit"[^>]*>(.*?)</tr>', re.DOTALL)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_VISIBILITY = re.compile(r"<r><c>setAttr</c>\s*<d>([^<]+)</d>\s*<d>visibility</d>\s*<d>([^<]+)</d>")
_REDIRECT = re.compile(r"<r><c>redirect</c>\s*<d>([^<]+)</d>")
_HATALI = re.compile(r"HATALI==&gt;\s*([^<]+)|HATALI==>\s*([^<]+)")
_ERROR_CODE = re.compile(r"Hata kodu\s*:\s*([^<]+)")

_REPORT_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_REPORT_CELL = re.compile(r"<t[hd]([^>]*)>(.*?)</t[hd]>", re.DOTALL | re.IGNORECASE)
_COLSPAN = re.compile(r'colspan="?(\d+)', re.IGNORECASE)
_ROWSPAN = re.compile(r'rowspan="?(\d+)', re.IGNORECASE)


def clean_text(value: str) -> str:
    """Strip tags/entities and collapse whitespace (Turkish labels match verbatim)."""
    text = _TAGS.sub("", value)
    text = _html.unescape(text).replace("\xa0", " ")
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class LabeledComponent:
    """One labelled radio/checkbox/button: its ZK id and visible label."""

    id: str
    label: str
    group: str | None = None


@dataclass(frozen=True)
class ListItem:
    """One ``zul.sel.Listitem``: its id, first-cell label and every cell text."""

    id: str
    label: str
    cells: tuple[str, ...] = ()


@dataclass(frozen=True)
class OuterList:
    """One listbox body sent by the server (header plus items)."""

    box_id: str
    header: str
    items: tuple[ListItem, ...] = ()


@dataclass(frozen=True)
class AuResponse:
    """A parsed AU response."""

    outer: tuple[OuterList, ...] = ()
    visible: dict[str, bool] = field(default_factory=dict)
    redirect: str | None = None
    errors: tuple[str, ...] = ()
    text: str = ""


class SessionExpired(ConnectorError):
    """The ZK session died; the caller must :meth:`LegacyZkClient.bootstrap` again."""

    def __init__(self, message: str) -> None:
        super().__init__(SOURCE_ERROR, message)


@dataclass(frozen=True)
class ReportGrid:
    """A dense report grid with the all-empty rows/columns removed."""

    rows: list[list[str]]
    encoding: str
    title: str

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    @property
    def height(self) -> int:
        return len(self.rows)


class LegacyZkClient:
    """Paced, retrying, raw-storing client for one legacy ZK page."""

    def __init__(
        self,
        page: str,
        *,
        base_url: str | None = None,
        institution: str = "tuik",
        dataset: str = "zk",
        settings_obj: Settings | None = None,
        settings_prefix: str = "tuik_turizm",
        store: ObjectStore | None = None,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        resolved = settings_obj or global_settings
        self.page = page
        self._base_url = (base_url or getattr(resolved, f"{settings_prefix}_base_url")).rstrip("/")
        self._page_url = f"{self._base_url}/{page}"
        self._au_url = f"{self._base_url}/zkau"
        self._timeout_s = getattr(resolved, f"{settings_prefix}_request_timeout_s")
        self._max_retries = getattr(resolved, f"{settings_prefix}_max_retries")
        self._retry_backoff_s = getattr(resolved, f"{settings_prefix}_retry_backoff_s")
        self.pause_s = getattr(resolved, f"{settings_prefix}_pause_s")
        self._stall_s = getattr(resolved, f"{settings_prefix}_stall_s")
        self._institution = institution
        self._dataset = dataset
        self._store = store
        self._sleeper = sleeper
        self._clock = clock
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        self._last_request_at: float | None = None
        self._desktop_id: str | None = None
        self._session_id: str | None = None
        self.radios: dict[str, LabeledComponent] = {}
        self.checkboxes: dict[str, LabeledComponent] = {}
        self.buttons: dict[str, LabeledComponent] = {}
        self.listboxes: set[str] = set()
        self._list_headers: dict[str, str] = {}
        self._items: dict[str, list[ListItem]] = {}
        self._visible: dict[str, bool] = {}
        self._raw_keys: list[str] = []
        self.retries = 0
        self.stalls = 0

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def take_raw_keys(self) -> list[str]:
        keys, self._raw_keys = self._raw_keys, []
        return keys

    def _store_raw(self, channel: str, payload: bytes, ext: str) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            key = store_raw(
                self._institution, self._dataset, channel, payload, ext, store=self._store
            )
            self._raw_keys.append(key)
            return key
        except Exception:  # noqa: BLE001 - raw storage must not break the data path
            logger.warning("zk raw store failed for %s", channel, exc_info=True)
            return None

    def _pause(self) -> None:
        if self._last_request_at is None:
            self._last_request_at = self._clock()
            return
        wait = self.pause_s - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleeper(wait)
        self._last_request_at = self._clock()

    @property
    def au_url(self) -> str:
        """The AU endpoint with the current servlet session (``;jsessionid``)."""
        if self._session_id:
            return f"{self._au_url};jsessionid={self._session_id}"
        return self._au_url

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._base_url,
            "Referer": self._page_url,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        channel: str,
        ext: str,
    ) -> httpx.Response:
        attempt = 0
        while True:
            self._pause()
            started = self._clock()
            try:
                response = self._http.request(method, url, content=content, headers=self._headers())
            except httpx.TimeoutException as exc:
                self.stalls += 1
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        TIMEOUT, f"zk {channel} timed out after {attempt + 1} attempts: {exc}"
                    ) from exc
                self.retries += 1
                logger.warning("zk timeout on %s (attempt %d)", channel, attempt + 1)
                self._sleeper(self._retry_backoff_s * (2**attempt))
                attempt += 1
                continue
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ConnectorError(
                        SOURCE_ERROR,
                        f"zk transport error after {attempt + 1} attempts: {exc}",
                    ) from exc
                self.retries += 1
                self._sleeper(self._retry_backoff_s * (2**attempt))
                attempt += 1
                continue

            if self._clock() - started >= self._stall_s:
                self.stalls += 1
                logger.warning("zk slow response on %s (possible silent throttle)", channel)

            if response.status_code in (200, 206):
                return response
            if response.status_code >= 500 and attempt < self._max_retries:
                self.retries += 1
                self._sleeper(self._retry_backoff_s * (2**attempt))
                attempt += 1
                continue
            raise ConnectorError(
                SOURCE_ERROR,
                f"HTTP {response.status_code}: {response.content[:200].decode('utf-8', 'replace')}",
            )

    def bootstrap(self) -> str:
        """GET the page, reset component maps and return the page HTML."""
        response = self._request(
            "GET", self._page_url, channel=f"bootstrap-{self.page}", ext="html"
        )
        text = response.content.decode("utf-8", errors="replace")
        self._store_raw(f"bootstrap-{self.page}", response.content, "html")
        session = _JSESSIONID.search(text)
        desktop = _DESKTOP_ID.search(text)
        if not desktop:
            raise ConnectorError(FORMAT_CHANGED, f"{self.page}: bootstrap has no ZK desktop id")
        self._session_id = session.group(1) if session else None
        self._desktop_id = desktop.group(1)
        self.radios = self._parse_components(text, _RADIO_SPAN)
        self.checkboxes = self._parse_components(text, _CKBOX_SPAN)
        self.buttons = self._parse_components(text, _BUTTON)
        for match in _BUTTON_INPUT.finditer(text):
            component_id, label = match.group(1), clean_text(match.group(2))
            if label:
                self.buttons.setdefault(label, LabeledComponent(id=component_id, label=label))
        self.listboxes = {match.group(1) for match in _LIBOX.finditer(text)}
        self._list_headers = {}
        self._items = {}
        self._visible = {}
        for outer in _parse_bootstrap_lists(text):
            self._list_headers[outer.box_id] = outer.header
            if outer.items:
                self._items[outer.box_id] = list(outer.items)
        return text

    @staticmethod
    def _parse_components(text: str, pattern: re.Pattern[str]) -> dict[str, LabeledComponent]:
        components: dict[str, LabeledComponent] = {}
        for match in pattern.finditer(text):
            component_id, body = match.group(1), match.group(2)
            label = clean_text(body)
            group_match = _INPUT_NAME.search(body)
            if not label:
                continue
            components[label] = LabeledComponent(
                id=component_id, label=label, group=(group_match.group(1) if group_match else None)
            )
        return components

    # -- events ------------------------------------------------------------

    def _post(self, command: str, uuid: str, *data: str, channel: str) -> AuResponse:
        if self._desktop_id is None:
            raise SessionExpired("ZK session is not bootstrapped")
        parts = [f"dtid={self._desktop_id}", f"cmd.0={command}", f"uuid.0={uuid}"]
        parts.extend(f"data.0={value}" for value in data)
        response = self._request(
            "POST",
            self.au_url,
            content="&".join(parts).encode("utf-8"),
            channel=channel,
            ext="txt",
        )
        self._store_raw(channel, response.content, "txt")
        text = response.content.decode("utf-8", errors="replace")
        if "<rs>" not in text and "<rs " not in text:
            raise SessionExpired(f"ZK responded without an <rs> envelope on {channel}")
        return self._parse_response(text)

    @staticmethod
    def _parse_response(text: str) -> AuResponse:
        outer: list[OuterList] = []
        for match in _OUTER.finditer(text):
            box_id, body = match.group(1), match.group(2)
            header_match = _HEADER.search(body)
            header = clean_text(header_match.group(1)).rstrip(":").strip() if header_match else ""
            items: list[ListItem] = []
            for item in _ITEM.finditer(body):
                item_id, item_body = item.group(1), item.group(2)
                cells = tuple(clean_text(cell) for cell in _TD.findall(item_body))
                if not cells:
                    continue
                items.append(ListItem(id=item_id, label=cells[0], cells=cells))
            outer.append(OuterList(box_id=box_id, header=header, items=tuple(items)))
        visible = {m.group(1): m.group(2) == "true" for m in _VISIBILITY.finditer(text)}
        redirect_match = _REDIRECT.search(text)
        errors = tuple(
            clean_text(next(group for group in match.groups() if group))
            for match in _HATALI.finditer(text)
        )
        return AuResponse(
            outer=tuple(outer),
            visible=visible,
            redirect=(redirect_match.group(1) if redirect_match else None),
            errors=errors,
            text=text,
        )

    def _record(self, response: AuResponse) -> AuResponse:
        for outer in response.outer:
            self.listboxes.add(outer.box_id)
            self._list_headers[outer.box_id] = outer.header
            self._items[outer.box_id] = list(outer.items)
        self._visible.update(response.visible)
        return response

    def _radio(self, label: str) -> LabeledComponent:
        component = self.radios.get(label)
        if component is None:
            raise ConnectorError(FORMAT_CHANGED, f"{self.page}: no radio labelled {label!r}")
        return component

    def check_radio(self, label: str) -> AuResponse:
        component = self._radio(label)
        return self._record(
            self._post("onCheck", component.id, "true", channel=f"{self.page}-radio-{_slug(label)}")
        )

    def check_box(self, label: str) -> AuResponse:
        component = self.checkboxes.get(label)
        if component is None:
            raise ConnectorError(FORMAT_CHANGED, f"{self.page}: no checkbox labelled {label!r}")
        return self._record(
            self._post("onCheck", component.id, "true", channel=f"{self.page}-check-{_slug(label)}")
        )

    def select_items(self, box_id: str, item_ids: Sequence[str]) -> AuResponse:
        if not item_ids:
            raise ConnectorError(SOURCE_ERROR, f"no items selected for listbox {box_id}")
        return self._record(
            self._post("onSelect", box_id, *item_ids, channel=f"{self.page}-select-{box_id}")
        )

    def click_button(self, label: str) -> AuResponse:
        component = self.buttons.get(label)
        if component is None:
            raise ConnectorError(FORMAT_CHANGED, f"{self.page}: no button labelled {label!r}")
        return self._record(
            self._post(
                "onClick", component.id, "49", "10", "", channel=f"{self.page}-click-{_slug(label)}"
            )
        )

    def visible_checkboxes(self) -> list[str]:
        return [
            label for label, component in self.checkboxes.items() if self._visible.get(component.id)
        ]

    def visible_listboxes(self) -> list[str]:
        return [
            box_id
            for box_id in self._list_headers
            if self._visible.get(box_id, True) and self._items.get(box_id)
        ]

    def list_header(self, box_id: str) -> str:
        return self._list_headers.get(box_id, "")

    def list_items(self, box_id: str) -> list[ListItem]:
        return list(self._items.get(box_id, []))

    def generate_report(self) -> tuple[bytes, str]:
        """Click "Raporu Oluştur", follow the redirect and return ``(body, url)``."""
        response = self.click_button("Raporu Oluştur")
        if response.errors:
            raise ConnectorError(SOURCE_ERROR, "; ".join(response.errors))
        if response.redirect is None:
            code = _ERROR_CODE.search(response.text)
            detail = code.group(1).strip() if code else "no redirect"
            raise ConnectorError(SOURCE_ERROR, f"{self.page}: report failed ({detail})")
        report = self._request("GET", response.redirect, channel=f"{self.page}-report", ext="html")
        self._store_raw(f"{self.page}-report", report.content, "html")
        return report.content, response.redirect


def _parse_bootstrap_lists(text: str) -> list[OuterList]:
    """Parse the listbox bodies rendered into a page's bootstrap HTML.

    Unlike an AU response, the bootstrap is a full component tree: every
    ``zul.sel.Libox`` appears once, with its ``!head`` header row and its
    ``zul.sel.Listitem`` body inline. Some applications (the election system)
    fill the first list server-side in the bootstrap itself, so the client must
    read it there instead of waiting for an ``outer`` response.
    """
    boxes: list[OuterList] = []
    matches = list(_LIBOX.finditer(text))
    for index, match in enumerate(matches):
        box_id = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        header_match = _HEADER.search(body)
        header = clean_text(header_match.group(1)).rstrip(":").strip() if header_match else ""
        items: list[ListItem] = []
        for item in _ITEM.finditer(body):
            cells = tuple(clean_text(cell) for cell in _TD.findall(item.group(2)))
            if not cells:
                continue
            items.append(ListItem(id=item.group(1), label=cells[0], cells=cells))
        boxes.append(OuterList(box_id=box_id, header=header, items=tuple(items)))
    return boxes


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")[:40] or "event"


# --- report grid ----------------------------------------------------------


def parse_turkish_number(text: str) -> Decimal | None:
    """Parse ``52.775.055`` / ``1,23`` / ``-`` into a ``Decimal`` (or ``None``)."""
    value = (text or "").strip().replace("\xa0", "")
    if value in ("", "-", "–", ".", ",", "NaN", "nan"):
        return None
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    elif "." in value:
        value = value.replace(".", "")
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def decode_report(content: bytes, encoding: str | None = None) -> tuple[str, str]:
    """Decode a legacy Excel-HTML report, honouring the declared charset."""
    if encoding is None:
        match = _CHARSET.search(content[:2000].decode("latin-1", errors="replace"))
        encoding = match.group(1) if match else DEFAULT_REPORT_ENCODING
    try:
        return content.decode(encoding), encoding
    except (LookupError, UnicodeDecodeError):
        return content.decode(DEFAULT_REPORT_ENCODING, errors="replace"), DEFAULT_REPORT_ENCODING


def resolve_grid(rows: Sequence[Sequence[tuple[str, int, int]]]) -> list[list[str | None]]:
    """Resolve ``(text, colspan, rowspan)`` cells into a rectangular grid."""
    grid: dict[tuple[int, int], str | None] = {}
    max_column = 0
    for r, cells in enumerate(rows):
        column = 0
        for text, colspan, rowspan in cells:
            while (r, column) in grid:
                column += 1
            for dr in range(max(1, rowspan)):
                for dc in range(max(1, colspan)):
                    grid[(r + dr, column + dc)] = text if (dr == 0 and dc == 0) else None
            column += max(1, colspan)
            max_column = max(max_column, column)
    return [[grid.get((r, c)) for c in range(max_column)] for r in range(len(rows))]


def _drop_empty(matrix: list[list[str | None]]) -> list[list[str]]:
    rows = [row for row in matrix if any(cell not in (None, "") for cell in row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [None] * (width - len(row)) for row in rows]
    keep = [c for c in range(width) if any(row[c] not in (None, "") for row in rows)]
    return [[str(row[c]) if row[c] is not None else "" for c in keep] for row in rows]


def parse_report_grid(content: bytes, encoding: str | None = None) -> ReportGrid:
    """Parse a legacy Excel-HTML report into a dense grid with spacer cells gone."""
    text, encoding = decode_report(content, encoding)
    parsed_rows: list[list[tuple[str, int, int]]] = []
    for row_html in _REPORT_ROW.findall(text):
        cells: list[tuple[str, int, int]] = []
        for attrs, body in _REPORT_CELL.findall(row_html):
            colspan = int(_COLSPAN.search(attrs).group(1)) if _COLSPAN.search(attrs) else 1
            rowspan = int(_ROWSPAN.search(attrs).group(1)) if _ROWSPAN.search(attrs) else 1
            cells.append((clean_text(body), colspan, rowspan))
        parsed_rows.append(cells)
    rows = _drop_empty(resolve_grid(parsed_rows))
    title = rows[0][0] if rows and rows[0] else ""
    return ReportGrid(rows=rows, encoding=encoding, title=title)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_REPORT_ENCODING",
    "USER_AGENT",
    "AuResponse",
    "LabeledComponent",
    "LegacyZkClient",
    "ListItem",
    "OuterList",
    "ReportGrid",
    "SessionExpired",
    "clean_text",
    "decode_report",
    "parse_report_grid",
    "parse_turkish_number",
    "resolve_grid",
]
