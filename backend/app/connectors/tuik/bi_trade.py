"""TÜİK bi.tuik Qlik Sense foreign-trade connector (GTS + ÖTS).

``bi.tuik.gov.tr`` is an anonymous Qlik Sense app. There is no login: a browser
``GET`` on the mashup page sets the session cookies, ``/qps/csrftoken`` returns
the CSRF token, and the engine is reached over a WebSocket using JSON-RPC 2.0.

Two apps are catalogued as two datasets (``TUIK_BI_GTS`` and ``TUIK_BI_OTS``):

- GTS (Genel Ticaret Sistemi, ``report_type=1``),
- ÖTS (Özel Ticaret Sistemi, ``report_type=2``), ÖTS lacks the customs, transport,
  payment, contract and invoice-currency dimensions.

Everything the app exposes is catalogued as a dimension (flow, partner, partner
group, the four alternative product classifications, province and — GTS only —
the five customs-related breakdowns). Values are fetched by selecting one code
per non-``_T`` dimension and reading a ``YIL``/``AY`` cube. This module never
writes to the database: the CLI upserts the catalogue and records observations
through the shared helpers in :mod:`app.connectors.base`.

Only one engine session is used at a time: parallel sessions make the engine
slower (measured 2 sessions -> each 2x slower; 4 -> 5x). Pings are disabled
because the engine does not answer them while computing a large cube.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
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
    store_raw,
)
from app.data.errors import SeriesDefinitionError

logger = logging.getLogger(__name__)

CHANNEL = "bi_qlik"
INSTITUTION = "tuik"
INSTITUTION_NAME = "Türkiye İstatistik Kurumu"

GTS = "gts"
OTS = "ots"
SYSTEM_KEYS = (GTS, OTS)

GTS_DATASET = "TUIK_BI_GTS"
OTS_DATASET = "TUIK_BI_OTS"

TOTAL_CODE = "_T"
TOTAL_LABEL = "Toplam"

# Only YIL and AY are true numeric fields; every code field must be selected by
# its text (passing qNumber on a dual text/numeric field silently fails to
# filter, measured 2026-09-30).
NUMERIC_FIELDS = frozenset({"YIL", "AY"})

FLOW_VALUES = {"X": "İhracat", "M": "İthalat"}
FLOW_CODES = {value: code for code, value in FLOW_VALUES.items()}

GROUP_PREFIXES = {"CG:": "ULKE_COGRAFI_GRUBU", "EG:": "ULKE_EKONOMI_GRUBU"}

HS_FIELD_BY_LENGTH = {2: "FASIL", 4: "TARIFE4", 6: "TARIFE6", 8: "TARIFE8", 12: "ISTPOZ"}

# The engine's HS fields drop leading zeros at every level: ``FASIL`` holds
# ``1`` … ``99`` (not ``01``), ``TARIFE4`` holds ``704`` (not ``0704``) and so on,
# while every other TÜİK channel and the GTİP classification keep the canonical
# zero-padded widths below. Codes are stored canonically in the catalogue, series
# keys and parents, and translated back to the engine value only for selection.
HS_CODE_WIDTHS = (2, 4, 6, 8, 12)


def _hs_canonical_width(length: int) -> int | None:
    for width in HS_CODE_WIDTHS:
        if width >= length:
            return width
    return None


SIMPLE_SELECTION_FIELDS = {
    "PARTNER": "ULKE_KODU",
    "PROVINCE": "IL_KODU",
    "CUSTOMS": "GUMRUK",
    "TRANSPORT": "TASIMA_SEKLI",
    "PAYMENT": "ODEME_SEKLI",
    "CONTRACT": "SOZLESME_KODU",
    "INVOICE_CURRENCY": "DOVIZ_KODU",
}

PRODUCT_DIMENSIONS = ("PRODUCT_HS", "PRODUCT_SITC", "PRODUCT_ISIC", "PRODUCT_BEC")
PARTNER_DIMENSIONS = ("PARTNER", "PARTNER_GROUP")

# BEC_1 is the app's four broad economic categories; the 2-digit BEC field also
# carries the 1-digit n.e.s. code ``7``, which is not one of them.
BEC_LEVEL1_CODES = frozenset({"1", "2", "3", "4"})

# ``MEASURE`` is the one dimension without a ``_T`` code: a series must name the
# measure it observes. Quantity measures are only meaningful for one product.
MEASURES: tuple[tuple[str, str, str, str | None], ...] = (
    ("USD", "ABD Doları (USD)", "Sum(DOLAR)", "US Dollar"),
    ("EUR", "Avro (EUR)", "Sum(EURO)", "Euro"),
    ("TRY", "Türk Lirası (TL)", "Sum(TL)", "Turkish Lira"),
    ("QTY1", "Miktar 1 (OLCU)", "Sum(MIKTAR_1)", None),
    ("QTY2", "Miktar 2 (OLCU)", "Sum(MIKTAR_2)", None),
)
MEASURE_EXPRESSIONS = {code: expression for code, _, expression, _ in MEASURES}
QTY_MEASURES = frozenset({"QTY1", "QTY2"})

MEASURE_FIELDS = {
    "USD": "DOLAR",
    "EUR": "EURO",
    "TRY": "TL",
    "QTY1": "MIKTAR_1",
    "QTY2": "MIKTAR_2",
}

DEFAULT_APP_IDS = {
    GTS: "bd4b4757-a3c9-45ba-b4fb-5c8d7e2d2c42",
    OTS: "8db826a9-59f2-4a33-a91e-88ca417dddf9",
}


@dataclass(frozen=True)
class BiSystem:
    """One Qlik app plus the metadata the catalogue needs."""

    key: str
    code: str
    app_id: str
    report_type: int
    name: str
    description: str
    gts_only: bool
    fact_table: str


@dataclass(frozen=True)
class SourceField:
    """One engine field feeding a dimension's code list."""

    code_field: str
    name_field: str | None = None
    code_prefix: str = ""
    level: int = 1


@dataclass(frozen=True)
class DimensionSpec:
    """A catalogued dimension and where its codes come from."""

    code: str
    label: str
    role: str
    kind: str = "filter"
    sources: tuple[SourceField, ...] = ()
    total: bool = True
    derive_parents: bool = False


@dataclass(frozen=True)
class HeadlineSpec:
    """One preloaded breakdown: one cube, split into series."""

    name: str
    label: str
    engine_dimensions: tuple[str, ...]
    mapping: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HeadlineSeries:
    """One series produced by a headline cube."""

    name: str
    codes: dict[str, str]
    points: list[tuple[date, Decimal | None]]
    raw_object_keys: list[str] = field(default_factory=list)


def get_system(key: str, settings_obj: Settings | None = None) -> BiSystem:
    """Resolve a system key (``gts``/``ots``) or dataset code to its metadata."""
    resolved = settings_obj or global_settings
    app_ids = {
        GTS: resolved.tuik_bi_gts_app_id,
        OTS: resolved.tuik_bi_ots_app_id,
    }
    systems = {
        GTS: BiSystem(
            key=GTS,
            code=GTS_DATASET,
            app_id=app_ids[GTS],
            report_type=1,
            name="TÜİK Dış Ticaret — Genel Ticaret Sistemi (GTS)",
            description=(
                "Aylık ihracat ve ithalat; ülke, ürün (GTİP/SITC/ISIC/BEC), il, "
                "gümrük, taşıma, ödeme ve sözleşme kırılımları (Qlik, bi.tuik)."
            ),
            gts_only=True,
            fact_table="DT_GENEL",
        ),
        OTS: BiSystem(
            key=OTS,
            code=OTS_DATASET,
            app_id=app_ids[OTS],
            report_type=2,
            name="TÜİK Dış Ticaret — Özel Ticaret Sistemi (ÖTS)",
            description=(
                "Aylık ihracat ve ithalat; ülke, ürün (GTİP/SITC/ISIC/BEC) ve il "
                "kırılımları (Qlik, bi.tuik). Gümrük/taşıma/ödeme/sözleşme ve "
                "döviz kırılımları yoktur."
            ),
            gts_only=False,
            fact_table="DT_GENEL",
        ),
    }
    candidate = key.strip()
    for system in systems.values():
        if candidate in (system.key, system.code):
            return system
    raise ConnectorError(NOT_FOUND, f"unknown TÜİK BI system {key!r} (want gts or ots)")


def all_systems(settings_obj: Settings | None = None) -> list[BiSystem]:
    return [get_system(key, settings_obj) for key in SYSTEM_KEYS]


def dimension_specs(system: BiSystem) -> list[DimensionSpec]:
    """Every non-time dimension of one system, in series-key order."""
    specs = [
        DimensionSpec(
            "FLOW", "Akış (İhracat/İthalat)", ROLE_OTHER, "flow", (SourceField("IHRITH"),)
        ),
        DimensionSpec(
            "PARTNER", "Ülke", ROLE_GEO, "filter", (SourceField("ULKE_KODU", "ULKE_ADI"),)
        ),
        DimensionSpec(
            "PARTNER_GROUP",
            "Ülke grubu (coğrafi/ekonomik)",
            ROLE_GEO,
            "filter",
            (
                SourceField("ULKE_COGRAFI_GRUBU", "ULKE_COGRAFI_ADI", code_prefix="CG:"),
                SourceField("ULKE_EKONOMI_GRUBU", "ULKE_EKONOMIK_ADI", code_prefix="EG:"),
            ),
        ),
        DimensionSpec(
            "PRODUCT_HS",
            "Ürün (GTİP: fasıl/tarife)",
            ROLE_OTHER,
            "product",
            (
                SourceField("FASIL", "FASIL_ADI", level=1),
                SourceField("TARIFE4", "TARIFE4_ADI", level=2),
                SourceField("TARIFE6", "TARIFE6_ADI", level=3),
                SourceField("TARIFE8", "TARIFE8_ADI", level=4),
                SourceField("ISTPOZ", "ISTPOZ_ADI", level=5),
            ),
            derive_parents=True,
        ),
        DimensionSpec(
            "PRODUCT_SITC",
            "Ürün (SITC Rev.4)",
            ROLE_OTHER,
            "product",
            tuple(SourceField(f"SITC4_{i}", f"SITC4_{i}_ADI", level=i) for i in range(1, 6)),
            derive_parents=True,
        ),
        DimensionSpec(
            "PRODUCT_ISIC",
            "Ürün (ISIC Rev.4)",
            ROLE_OTHER,
            "product",
            tuple(SourceField(f"ISIC4_{i}", f"ISIC4_{i}_ADI", level=i) for i in range(1, 5)),
            derive_parents=True,
        ),
        DimensionSpec(
            "PRODUCT_BEC",
            "Ürün (BEC)",
            ROLE_OTHER,
            "product",
            (
                SourceField("BEC_1", "BEC_1_ADI", level=1),
                SourceField("BEC", "BEC_ADI", level=2),
            ),
        ),
        DimensionSpec("PROVINCE", "İl", ROLE_GEO, "filter", (SourceField("IL_KODU", "IL_ADI"),)),
    ]
    if system.gts_only:
        specs += [
            DimensionSpec(
                "CUSTOMS",
                "Gümrük müdürlüğü",
                ROLE_GEO,
                "filter",
                (SourceField("GUMRUK", "GUMRUK_ADI"),),
            ),
            DimensionSpec(
                "TRANSPORT",
                "Taşıma şekli",
                ROLE_OTHER,
                "filter",
                (SourceField("TASIMA_SEKLI", "TASIMA_SEKLI_ADI"),),
            ),
            DimensionSpec(
                "PAYMENT",
                "Ödeme şekli",
                ROLE_OTHER,
                "filter",
                (SourceField("ODEME_SEKLI", "ODEME_SEKLI_ADI"),),
            ),
            DimensionSpec(
                "CONTRACT",
                "Sözleşme",
                ROLE_OTHER,
                "filter",
                (SourceField("SOZLESME_KODU", "SOZLESME_ADI"),),
            ),
            DimensionSpec(
                "INVOICE_CURRENCY",
                "Fatura para birimi",
                ROLE_OTHER,
                "filter",
                (SourceField("DOVIZ_KODU", "DOVIZ_ADI"),),
            ),
        ]
    specs.append(
        DimensionSpec("MEASURE", "Ölçü (döviz/miktar)", ROLE_OTHER, "measure", (), total=False)
    )
    return specs


def dimension_codes(system: BiSystem) -> list[str]:
    return [spec.code for spec in dimension_specs(system)]


# --- pure selection/validation helpers ------------------------------------


def to_catalog_code(dimension: str, engine_value: str) -> str:
    """Engine value -> our catalogue code (HS levels padded to their width)."""
    if dimension == "PRODUCT_HS" and engine_value.isdigit():
        width = _hs_canonical_width(len(engine_value))
        if width is not None:
            return engine_value.zfill(width)
    return engine_value


def to_engine_value(dimension: str, catalog_code: str) -> str:
    """Our catalogue code -> engine value (HS leading zeros dropped)."""
    if dimension == "PRODUCT_HS" and catalog_code.isdigit():
        return catalog_code.lstrip("0") or "0"
    return catalog_code


def product_field(dimension: str, code: str) -> str:
    """The engine field a product code belongs to (its level)."""
    if dimension == "PRODUCT_HS":
        field = HS_FIELD_BY_LENGTH.get(len(code))
    elif dimension == "PRODUCT_SITC":
        field = f"SITC4_{len(code)}" if 1 <= len(code) <= 5 else None
    elif dimension == "PRODUCT_ISIC":
        field = f"ISIC4_{len(code)}" if 1 <= len(code) <= 4 else None
    elif dimension == "PRODUCT_BEC":
        field = "BEC_1" if code in BEC_LEVEL1_CODES else "BEC"
    else:
        field = None
    if field is None:
        raise SeriesDefinitionError(f"code {code!r} is not a valid {dimension} code")
    return field


def selection_for(dimension: str, code: str) -> tuple[str, str] | None:
    """The ``(engine field, engine value)`` selection for one dimension code."""
    if code == TOTAL_CODE:
        return None
    if dimension == "FLOW":
        value = FLOW_VALUES.get(code)
        if value is None:
            raise SeriesDefinitionError(f"FLOW code must be X, M or {TOTAL_CODE}, got {code!r}")
        return "IHRITH", value
    if dimension in SIMPLE_SELECTION_FIELDS:
        return SIMPLE_SELECTION_FIELDS[dimension], code
    if dimension == "PARTNER_GROUP":
        for prefix, engine_field in GROUP_PREFIXES.items():
            if code.startswith(prefix):
                return engine_field, code[len(prefix) :]
        raise SeriesDefinitionError(f"PARTNER_GROUP code {code!r} needs a CG:/EG: prefix")
    if dimension in PRODUCT_DIMENSIONS:
        return product_field(dimension, code), to_engine_value(dimension, code)
    raise SeriesDefinitionError(f"dimension {dimension!r} is not selectable")


def complete_codes(system: BiSystem, codes: Mapping[str, str]) -> dict[str, str]:
    """Fill every omitted filtering dimension with ``_T``; require a MEASURE."""
    result = {code: str(value) for code, value in codes.items()}
    for spec in dimension_specs(system):
        if spec.code in result:
            continue
        if spec.kind == "measure":
            raise SeriesDefinitionError(
                "MEASURE is required; choose one of " + ", ".join(MEASURE_EXPRESSIONS)
            )
        if spec.total:
            result[spec.code] = TOTAL_CODE
    return result


def validate_codes(system: BiSystem, codes: Mapping[str, str]) -> None:
    """Reject keys with several product/partner dimensions or a bare QTY measure."""
    products = [dim for dim in PRODUCT_DIMENSIONS if codes.get(dim, TOTAL_CODE) != TOTAL_CODE]
    if len(products) > 1:
        raise SeriesDefinitionError(
            f"at most one product dimension may be selected, got {products}"
        )
    if (
        codes.get("PARTNER", TOTAL_CODE) != TOTAL_CODE
        and codes.get("PARTNER_GROUP", TOTAL_CODE) != TOTAL_CODE
    ):
        raise SeriesDefinitionError("PARTNER and PARTNER_GROUP cannot both be selected")
    measure = codes.get("MEASURE")
    if measure in (None, TOTAL_CODE) or measure not in MEASURE_EXPRESSIONS:
        raise SeriesDefinitionError(
            f"MEASURE must be one of {sorted(MEASURE_EXPRESSIONS)}, got {measure!r}"
        )
    if measure in QTY_MEASURES and not products:
        raise SeriesDefinitionError(
            f"{measure} needs one product dimension selected (units come from OLCU)"
        )


def derive_parents(codes: Sequence[str]) -> dict[str, str | None]:
    """Map each code to its longest proper prefix also present (hierarchy)."""
    code_set = set(codes)
    parents: dict[str, str | None] = {}
    for code in codes:
        parent: str | None = None
        for end in range(len(code) - 1, 0, -1):
            prefix = code[:end]
            if prefix in code_set:
                parent = prefix
                break
        parents[code] = parent
    return parents


def _evaluated_value(result: Mapping[str, Any]) -> Any:
    """Unwrap an ``EvaluateEx``/``Evaluate`` response to its scalar value.

    The engine returns ``{"qValue": {"qText": ..., "qNumber": ...}}`` for
    ``EvaluateEx`` and ``{"qReturn": "<value>"}`` for ``Evaluate``.
    """
    value = result.get("qValue")
    if value is None:
        value = result.get("qReturn")
    if isinstance(value, dict):
        return value.get("qText", value.get("qNumber"))
    return value


def _parse_number(text: str) -> Decimal | None:
    value = (text or "").strip()
    if value in ("", "-", "NaN", "nan"):
        return None
    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def month_period(year_text: str, month_text: str) -> date | None:
    """Convert a ``YIL``/``AY`` pair to the first day of the month, when valid."""
    try:
        year = int((year_text or "").strip())
        month = int((month_text or "").strip())
    except (TypeError, ValueError):
        return None
    if not (1900 <= year <= 2200 and 1 <= month <= 12):
        return None
    return date(year, month, 1)


def split_headline(
    rows: Sequence[Sequence[str]], spec: HeadlineSpec
) -> dict[tuple[str, ...], list[tuple[date, Decimal | None]]]:
    """Split one headline cube's rows into ``(engine values) -> points``."""
    width = len(spec.engine_dimensions)
    series: dict[tuple[str, ...], list[tuple[date, Decimal | None]]] = {}
    for row in rows:
        if len(row) < width + 3:
            continue
        period = month_period(row[0], row[1])
        if period is None:
            continue
        key = tuple(row[2 : 2 + width])
        # The engine adds a null bucket (text ``-``) to every dimension; it is not
        # a real breakdown and never becomes a series.
        if any(value in ("", "-") for value in key):
            continue
        series.setdefault(key, []).append((period, _parse_number(row[-1])))
    return series


def code_for(dimension: str, value: str) -> str:
    """Our dimension code for one engine value (only FLOW and HS chapters differ)."""
    if dimension == "FLOW":
        return FLOW_CODES.get(value, value)
    return to_catalog_code(dimension, value)


def headline_code(dimension: str, value: str) -> str:
    return code_for(dimension, value)


HEADLINES: tuple[HeadlineSpec, ...] = (
    HeadlineSpec("flow", "Akış toplamları", ("IHRITH",), (("IHRITH", "FLOW"),)),
    HeadlineSpec(
        "partner",
        "Ülke kırılımı",
        ("IHRITH", "ULKE_KODU"),
        (("IHRITH", "FLOW"), ("ULKE_KODU", "PARTNER")),
    ),
    HeadlineSpec(
        "product_hs",
        "Ürün (GTİP fasıl) kırılımı",
        ("IHRITH", "FASIL"),
        (("IHRITH", "FLOW"), ("FASIL", "PRODUCT_HS")),
    ),
    HeadlineSpec(
        "product_bec",
        "Ürün (BEC_1) kırılımı",
        ("IHRITH", "BEC_1"),
        (("IHRITH", "FLOW"), ("BEC_1", "PRODUCT_BEC")),
    ),
    HeadlineSpec(
        "province",
        "İl kırılımı",
        ("IHRITH", "IL_KODU"),
        (("IHRITH", "FLOW"), ("IL_KODU", "PROVINCE")),
    ),
)

HEADLINE_BY_NAME = {spec.name: spec for spec in HEADLINES}


# --- engine client --------------------------------------------------------


class BiConnection(Protocol):
    """The tiny slice of the websockets sync API the client needs."""

    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, Mapping[str, str]], BiConnection]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


class _SocketDropped(Exception):
    """Internal: the engine socket closed mid-request; retry after reconnecting."""


@dataclass(frozen=True)
class _Session:
    cookies: str
    csrf_token: str
    xrf_key: str


class BiTradeClient:
    """One-at-a-time, raw-storing synchronous facade over the Qlik engine."""

    def __init__(
        self,
        *,
        settings_obj: Settings | None = None,
        store: ObjectStore | None = None,
        http_client: httpx.Client | None = None,
        connection_factory: ConnectionFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = settings_obj or global_settings
        self._base_url = resolved.tuik_bi_base_url.rstrip("/")
        self._timeout_s = resolved.tuik_bi_request_timeout_s
        self._open_timeout_s = resolved.tuik_bi_open_timeout_s
        self._page_cells = max(1, resolved.tuik_bi_page_cells)
        self._max_size = resolved.tuik_bi_max_size_bytes
        self._max_retries = resolved.tuik_bi_max_retries
        self._retry_backoff_s = resolved.tuik_bi_retry_backoff_s
        self._settings = resolved
        self._store = store
        self._sleeper = sleeper
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            timeout=resolved.tuik_request_timeout_s, follow_redirects=True
        )
        self._connect = connection_factory
        self._conn: BiConnection | None = None
        self._system: BiSystem | None = None
        self._doc: int | None = None
        self._counter = 0
        self._selections: dict[str, str] = {}
        self._raw_keys: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def system(self) -> BiSystem | None:
        return self._system

    def close(self) -> None:
        self._close_connection()
        if self._owns_http:
            self._http.close()

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing must never raise at teardown
                pass
        self._conn = None
        self._doc = None

    def _mashup_url(self, report_type: int) -> str:
        return f"{self._base_url}/extensions/tuik-mashup/index.html?report_type={report_type}"

    def bootstrap(self, report_type: int) -> _Session:
        """Set the anonymous cookies and read the CSRF token (a plain GET pair)."""
        ua = _USER_AGENT
        response = self._http.get(self._mashup_url(report_type), headers={"User-Agent": ua})
        if response.status_code >= 400:
            raise ConnectorError(
                SOURCE_ERROR, f"bi.tuik mashup page answered HTTP {response.status_code}"
            )
        cookies = "; ".join(f"{name}={value}" for name, value in response.cookies.items())
        xrf_key = secrets.token_hex(8)
        token_response = self._http.get(
            f"{self._base_url}/qps/csrftoken?xrfkey={xrf_key}",
            headers={"User-Agent": ua, "x-qlik-xrfkey": xrf_key, "Cookie": cookies},
        )
        token = token_response.headers.get("qlik-csrf-token")
        if not token:
            raise ConnectorError(SOURCE_ERROR, "bi.tuik did not return a qlik-csrf-token")
        return _Session(cookies=cookies, csrf_token=token, xrf_key=xrf_key)

    def open(self, system: BiSystem | str) -> None:
        """Open the engine session for one system (idempotent)."""
        resolved = system if isinstance(system, BiSystem) else get_system(system, self._settings)
        if self._conn is not None and self._system is not None and self._system.key == resolved.key:
            return
        self._close_connection()
        self._system = resolved
        self._selections = {}
        self._open_socket(resolved)
        self._open_doc(resolved)

    def _open_socket(self, system: BiSystem) -> None:
        self._close_connection()
        session = self.bootstrap(system.report_type)
        url = (
            f"wss://bi.tuik.gov.tr/app/{system.app_id}"
            f"?reloadUri={self._mashup_url(system.report_type)}"
            f"&qlik-csrf-token={session.csrf_token}"
        )
        headers = {"Cookie": session.cookies, "User-Agent": _USER_AGENT, "Origin": self._base_url}
        self._conn = self._new_connection(url, headers)

    def _new_connection(self, url: str, headers: Mapping[str, str]) -> BiConnection:
        if self._connect is not None:
            return self._connect(url, headers)
        from websockets.sync.client import connect

        return connect(
            url,
            additional_headers=dict(headers),
            ping_interval=None,
            open_timeout=self._open_timeout_s,
            max_size=self._max_size,
            close_timeout=10,
        )

    def _open_doc(self, system: BiSystem) -> None:
        result = self._request("OpenDoc", -1, [system.app_id, "", "", "", False])
        self._doc = int(result["qReturn"]["qHandle"])

    # -- JSON-RPC ----------------------------------------------------------

    def _request(self, method: str, handle: int, params: list[Any]) -> dict[str, Any]:
        if self._conn is None:
            raise ConnectorError(SOURCE_ERROR, "engine session is not open")
        self._counter += 1
        message_id = self._counter
        message = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": method,
                "handle": handle,
                "params": params,
            }
        )
        try:
            self._conn.send(message)
        except Exception as exc:  # noqa: BLE001 - normalised to a dropped-socket retry
            raise _SocketDropped(str(exc)) from exc
        while True:
            try:
                raw = self._conn.recv(timeout=self._timeout_s)
            except TimeoutError as exc:
                raise ConnectorError(
                    TIMEOUT, f"bi.tuik engine call {method} timed out after {self._timeout_s}s"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - normalised to a dropped-socket retry
                raise _SocketDropped(str(exc)) from exc
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise ConnectorError(FORMAT_CHANGED, "bi.tuik engine sent non-JSON") from exc
            if data.get("id") != message_id:
                continue
            if "error" in data:
                raise ConnectorError(
                    SOURCE_ERROR, f"bi.tuik engine {method} error: {data['error']}"
                )
            return data.get("result", {})

    def call(self, method: str, handle: int, params: list[Any]) -> dict[str, Any]:
        """One engine call, reconnecting once per dropped socket."""
        attempt = 0
        while True:
            try:
                return self._request(method, handle, params)
            except _SocketDropped as exc:
                if attempt >= self._max_retries or self._system is None:
                    raise ConnectorError(SOURCE_ERROR, f"bi.tuik socket dropped: {exc}") from exc
                logger.warning("bi_qlik: socket dropped on %s; reconnecting", method)
                self._sleeper(self._retry_backoff_s * (2**attempt))
                self._open_socket(self._system)
                self._open_doc(self._system)
                for field_name, value in self._selections.items():
                    self._select(field_name, value)
                attempt += 1

    def _doc_handle(self) -> int:
        if self._doc is None:
            raise ConnectorError(SOURCE_ERROR, "engine document is not open")
        return self._doc

    def clear_all(self) -> None:
        self._selections = {}
        self.call("ClearAll", self._doc_handle(), [False, "$"])

    def field_handle(self, name: str) -> int:
        result = self.call("GetField", self._doc_handle(), [name])
        return int(result["qReturn"]["qHandle"])

    def select_field(self, name: str, value: str) -> None:
        """Select (and remember) one field value so a later cube is filtered."""
        self._select(name, value)
        self._selections[name] = value

    def _select(self, name: str, value: str) -> None:
        handle = self.field_handle(name)
        if name in NUMERIC_FIELDS:
            field_value: dict[str, Any] = {
                "qText": value,
                "qIsNumeric": True,
                "qNumber": float(value),
            }
        else:
            field_value = {"qText": value, "qIsNumeric": False}
        self.call("SelectValues", handle, [[field_value], False, False])
        # A code the app does not contain is silently ignored: the engine returns
        # success and the cube stays unfiltered. Confirm the selection really
        # landed and fail loudly otherwise.
        if self.selected_count(name) < 1:
            raise ConnectorError(
                NOT_FOUND, f"{name}={value!r} is not a value of the engine field {name}"
            )

    def selected_count(self, name: str) -> int:
        """The number of selected values of one field (``GetSelectedCount``)."""
        result = self.call("EvaluateEx", self._doc_handle(), [f"GetSelectedCount([{name}])"])
        raw = _evaluated_value(result)
        try:
            return int(float(raw))
        except (TypeError, ValueError) as exc:
            raise ConnectorError(
                FORMAT_CHANGED, f"bi.tuik returned no selection count for field {name}"
            ) from exc

    # -- raw storage -------------------------------------------------------

    def _store_page(self, dataset: str, channel: str, payload: bytes) -> str | None:
        if self._store is None or not payload:
            return None
        try:
            return store_raw(INSTITUTION, dataset, channel, payload, "json", store=self._store)
        except Exception:  # noqa: BLE001 - raw storage must never break the data path
            logger.warning("bi_qlik raw store failed for %s/%s", dataset, channel, exc_info=True)
            return None

    def take_raw_keys(self) -> list[str]:
        keys, self._raw_keys = self._raw_keys, []
        return keys

    # -- engine objects ----------------------------------------------------

    def tables(self, dataset: str) -> list[dict[str, Any]]:
        """The app's tables (with their fields and row counts), stored raw."""
        result = self.call(
            "GetTablesAndKeys",
            self._doc_handle(),
            [{"qcx": 10000, "qcy": 10000}, {"qcx": 0, "qcy": 0}, 20, False, True],
        )
        payload = json.dumps(result).encode("utf-8")
        key = self._store_page(dataset, f"{CHANNEL}-tables", payload)
        if key:
            self._raw_keys.append(key)
        return list(result.get("qtr") or [])

    def fields(self, dataset: str) -> list[str]:
        names: list[str] = []
        for table in self.tables(dataset):
            for entry in table.get("qFields") or []:
                name = entry.get("qName")
                if name:
                    names.append(str(name))
        return names

    def row_count(self, dataset: str) -> int | None:
        counts = [int(table.get("qNoOfRows") or 0) for table in self.tables(dataset)]
        return max(counts) if counts else None

    def cube_rows(
        self,
        dimensions: Sequence[str],
        measure: str,
        *,
        dataset: str,
        suppress_zero: bool = True,
    ) -> list[list[str]]:
        """Run a hypercube and return each row's cell texts."""
        return [
            [str(cell.get("qText", "")) for cell in row]
            for row in self.cube_cells(
                dimensions, measure, dataset=dataset, suppress_zero=suppress_zero
            )
        ]

    def cube_cells(
        self,
        dimensions: Sequence[str],
        measure: str,
        *,
        dataset: str,
        suppress_zero: bool = True,
    ) -> list[list[dict[str, Any]]]:
        """Run a hypercube, paging at ``page_cells`` and storing each page raw."""
        if not dimensions:
            raise ConnectorError(FORMAT_CHANGED, "a cube needs at least one dimension")
        width = len(dimensions) + 1
        height = max(1, self._page_cells // width)
        document = self._doc_handle()
        result = self.call(
            "CreateSessionObject",
            document,
            [
                {
                    "qInfo": {"qType": "karven"},
                    "qHyperCubeDef": {
                        "qDimensions": [{"qDef": {"qFieldDefs": [name]}} for name in dimensions],
                        "qMeasures": [{"qDef": {"qDef": measure}}],
                        "qSuppressZero": suppress_zero,
                    },
                }
            ],
        )
        handle = int(result["qReturn"]["qHandle"])
        rows: list[list[dict[str, Any]]] = []
        try:
            layout = self.call("GetLayout", handle, [])
            size = layout["qLayout"]["qHyperCube"]["qSize"]
            total = int(size.get("qcy") or 0)
            top = 0
            while top < total:
                page_height = min(height, total - top)
                data = self.call(
                    "GetHyperCubeData",
                    handle,
                    [
                        "/qHyperCubeDef",
                        [{"qTop": top, "qLeft": 0, "qWidth": width, "qHeight": page_height}],
                    ],
                )
                page = (data.get("qDataPages") or [{}])[0]
                payload = json.dumps(page).encode("utf-8")
                key = self._store_page(dataset, f"{CHANNEL}-cube", payload)
                if key:
                    self._raw_keys.append(key)
                for row in page.get("qMatrix") or []:
                    rows.append(list(row))
                top += page_height
        finally:
            self._destroy(handle)
        return rows

    def list_field_pairs(
        self, code_field: str, name_field: str | None, *, dataset: str
    ) -> list[tuple[str, str, int]]:
        """Every ``(code, label, count)`` of a code field (or a code/name pair).

        The engine appends a null bucket (``qElemNumber < 0``, text ``-``) to
        every cube; it is not a code and is skipped.
        """
        dimensions = [code_field, name_field] if name_field else [code_field]
        rows = self.cube_cells(dimensions, "Count(1)", dataset=dataset, suppress_zero=False)
        pairs: dict[str, tuple[str, str, int]] = {}
        for row in rows:
            if not row:
                continue
            element = row[0].get("qElemNumber")
            if isinstance(element, int) and element < 0:
                continue
            code = str(row[0].get("qText", "")).strip()
            if not code or code == "-":
                continue
            label = str(row[1].get("qText", "")).strip() if name_field and len(row) > 1 else code
            count_cell = row[2] if name_field else row[1]
            try:
                count = int(count_cell.get("qText") or 0)
            except (AttributeError, ValueError):
                count = 0
            existing = pairs.get(code)
            if existing is None or count > existing[2]:
                pairs[code] = (code, label, count)
        return list(pairs.values())

    def _destroy(self, handle: int) -> None:
        try:
            # The engine wants the object id as a single string, not an array.
            self.call("DestroySessionObject", self._doc_handle(), [str(handle)])
        except ConnectorError:
            logger.warning("bi_qlik: could not destroy session object %s", handle, exc_info=True)


# --- connector ------------------------------------------------------------


class BiTradeConnector(SourceConnector):
    """Lists the two TÜİK BI datasets and fetches/loads their series."""

    institution_code = INSTITUTION
    institution_name = INSTITUTION_NAME
    channel = CHANNEL

    def __init__(
        self,
        *,
        client: BiTradeClient | None = None,
        store: ObjectStore | None = None,
        settings_obj: Settings | None = None,
    ) -> None:
        self._settings = settings_obj or global_settings
        if client is None:
            if store is None:
                store = MinioObjectStore(self._settings)
            client = BiTradeClient(settings_obj=self._settings, store=store)
        self._client = client

    @property
    def client(self) -> BiTradeClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def list_datasets(self) -> Iterator[DatasetMeta]:
        for system in all_systems(self._settings):
            yield self.dataset_meta(system)

    # -- catalog -----------------------------------------------------------

    def dataset_meta(self, system: BiSystem | str) -> DatasetMeta:
        """Build the full catalogue (dimensions + code lists) for one system."""
        resolved = system if isinstance(system, BiSystem) else get_system(system, self._settings)
        self._client.open(resolved)
        # A leftover selection would shrink the code lists; start clean.
        self._client.clear_all()
        tables = self._client.tables(resolved.code)
        available = {
            str(field["qName"])
            for table in tables
            for field in table.get("qFields") or []
            if field.get("qName")
        }
        specs = dimension_specs(resolved)
        missing = self._missing_fields(resolved, specs, available)
        if missing:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{resolved.code}: app {resolved.app_id} no longer exposes {missing}",
            )
        row_counts = [int(table.get("qNoOfRows") or 0) for table in tables]
        dimensions: list[DimensionMeta] = []
        for position, spec in enumerate(specs):
            dimensions.append(self._dimension_meta(resolved, spec, position))
        dimensions.append(
            DimensionMeta(
                code="TIME_PERIOD",
                label="Dönem",
                position=len(specs),
                role=ROLE_TIME,
                codes=[],
                attributes={"frequency": "monthly"},
            )
        )
        coverage_start, coverage_end = self._coverage(resolved)
        return DatasetMeta(
            external_code=resolved.code,
            name=resolved.name,
            description=resolved.description,
            source_category="Dış Ticaret İstatistikleri",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            obs_count=max(row_counts) if row_counts else None,
            attributes={
                "channel": CHANNEL,
                "system": resolved.key,
                "app_id": resolved.app_id,
                "report_type": resolved.report_type,
                "fact_table": resolved.fact_table,
                "default_frequency": "monthly",
                "measures": {code: MEASURE_FIELDS[code] for code in MEASURE_EXPRESSIONS},
            },
            dimensions=dimensions,
        )

    @staticmethod
    def _missing_fields(
        system: BiSystem, specs: Sequence[DimensionSpec], available: set[str]
    ) -> list[str]:
        required: list[str] = ["YIL", "AY"]
        for spec in specs:
            if spec.kind == "measure":
                continue
            for source in spec.sources:
                required.append(source.code_field)
                if source.name_field:
                    required.append(source.name_field)
        return sorted({field for field in required if field not in available})

    def _dimension_meta(
        self, system: BiSystem, spec: DimensionSpec, position: int
    ) -> DimensionMeta:
        codes: list[DimensionCodeMeta] = []
        if spec.total:
            codes.append(DimensionCodeMeta(code=TOTAL_CODE, label=TOTAL_LABEL, is_default=True))
        if spec.kind == "measure":
            for code, label, _expression, unit in MEASURES:
                attributes: dict[str, Any] = {"measure": MEASURE_FIELDS[code]}
                if unit:
                    attributes["unit"] = unit
                attributes["quantity"] = code in QTY_MEASURES
                codes.append(DimensionCodeMeta(code=code, label=label, attributes=attributes))
            return DimensionMeta(
                code=spec.code, label=spec.label, position=position, role=spec.role, codes=codes
            )
        entries: dict[str, tuple[str, int]] = {}
        for source in spec.sources:
            for code, label, _count in self._client.list_field_pairs(
                source.code_field, source.name_field, dataset=system.code
            ):
                full_code = source.code_prefix + code_for(spec.code, code)
                if full_code not in entries:
                    entries[full_code] = (label or code, source.level)
        all_codes = list(entries)
        parents = derive_parents(all_codes) if spec.derive_parents else {c: None for c in all_codes}
        for code in all_codes:
            label, level = entries[code]
            codes.append(
                DimensionCodeMeta(
                    code=code,
                    label=label,
                    parent_code=parents.get(code) or None,
                    attributes={"level": level, "field": self._field_for(spec, code)},
                )
            )
        return DimensionMeta(
            code=spec.code, label=spec.label, position=position, role=spec.role, codes=codes
        )

    @staticmethod
    def _field_for(spec: DimensionSpec, code: str) -> str:
        try:
            selection = selection_for(spec.code, code)
        except SeriesDefinitionError:
            return ""
        return selection[0] if selection is not None else ""

    def _coverage(self, system: BiSystem) -> tuple[date | None, date | None]:
        pairs = self._client.list_field_pairs("YIL", None, dataset=system.code)
        years: list[int] = []
        for code, _label, _count in pairs:
            try:
                years.append(int(code))
            except ValueError:
                continue
        if not years:
            return None, None
        return date(min(years), 1, 1), date(max(years), 12, 1)

    # -- fetch -------------------------------------------------------------

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        system = get_system(dataset_code, self._settings)
        completed = complete_codes(system, codes)
        validate_codes(system, completed)
        self._client.open(system)
        self._client.clear_all()
        for spec in dimension_specs(system):
            if spec.kind == "measure":
                continue
            selection = selection_for(spec.code, completed.get(spec.code, TOTAL_CODE))
            if selection is not None:
                self._client.select_field(*selection)
        measure = MEASURE_EXPRESSIONS[completed["MEASURE"]]
        rows = self._client.cube_rows(["YIL", "AY"], measure, dataset=system.code)
        if not rows:
            raise ConnectorError(EMPTY, f"{system.code}: the selection returned no rows")
        points: list[tuple[date, Decimal | None]] = []
        for row in rows:
            period = month_period(row[0], row[1]) if len(row) >= 3 else None
            if period is None or period < start:
                continue
            points.append((period, _parse_number(row[-1])))
        points.sort()
        raw_keys = self._client.take_raw_keys()
        self._client.clear_all()
        return FetchResult(
            external_code=external_code(system, completed),
            points=points,
            raw_object_keys=raw_keys,
            channel=CHANNEL,
        )

    # -- headlines ---------------------------------------------------------

    def headline_series(
        self, system: BiSystem | str, *, only: Sequence[str] | None = None
    ) -> list[HeadlineSeries]:
        """Load every preconfigured headline breakdown, one cube each."""
        resolved = system if isinstance(system, BiSystem) else get_system(system, self._settings)
        specs = HEADLINES if not only else tuple(HEADLINE_BY_NAME[name] for name in only)
        self._client.open(resolved)
        self._client.clear_all()
        series: list[HeadlineSeries] = []
        for spec in specs:
            dimensions = ["YIL", "AY", *spec.engine_dimensions]
            rows = self._client.cube_rows(dimensions, "Sum(DOLAR)", dataset=resolved.code)
            raw_keys = self._client.take_raw_keys()
            split = split_headline(rows, spec)
            for key, points in split.items():
                codes = {dimension: TOTAL_CODE for dimension in dimension_codes(resolved)}
                codes["MEASURE"] = "USD"
                for (_engine_field, dimension), value in zip(spec.mapping, key, strict=True):
                    codes[dimension] = headline_code(dimension, value)
                series.append(
                    HeadlineSeries(
                        name=resolved.name,
                        codes=codes,
                        points=points,
                        raw_object_keys=list(raw_keys),
                    )
                )
        self._client.clear_all()
        return series


def external_code(system: BiSystem, codes: Mapping[str, str]) -> str:
    """The series key shape ``<dataset>:<code in dimension order>``."""
    ordered = [codes[spec.code] for spec in dimension_specs(system)]
    return f"{system.code}:" + ".".join(ordered)


__all__ = [
    "BEC_LEVEL1_CODES",
    "CHANNEL",
    "DEFAULT_APP_IDS",
    "GTS",
    "GTS_DATASET",
    "HEADLINES",
    "HEADLINE_BY_NAME",
    "HS_CODE_WIDTHS",
    "MEASURES",
    "MEASURE_EXPRESSIONS",
    "NUMERIC_FIELDS",
    "OTS",
    "OTS_DATASET",
    "PRODUCT_DIMENSIONS",
    "SYSTEM_KEYS",
    "TOTAL_CODE",
    "BiSystem",
    "BiTradeClient",
    "BiTradeConnector",
    "DimensionSpec",
    "FetchResult",
    "HeadlineSeries",
    "HeadlineSpec",
    "SourceField",
    "all_systems",
    "code_for",
    "complete_codes",
    "derive_parents",
    "dimension_codes",
    "dimension_specs",
    "external_code",
    "get_system",
    "headline_code",
    "month_period",
    "product_field",
    "selection_for",
    "split_headline",
    "to_catalog_code",
    "to_engine_value",
    "validate_codes",
]
