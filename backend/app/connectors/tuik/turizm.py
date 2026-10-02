"""TÜİK tourism statistics (``biruni.tuik.gov.tr/turizmapp``) connector.

``turizmapp`` is a legacy ZK "DHTML" application with three anonymous pages:

- ``sinir.zul``   — border arrivals/departures (foreign visitors, citizens),
- ``cikis.zul``   — departing residents (visitor profile, accommodation,
  spending, tourism income),
- ``giris.zul``   — returning residents (profile, accommodation, spending,
  tourism expenditure).

The catalog walks each form path without generating a report (lists only) and
stores the datasets plus their complete code lists. Every reachable radio
combination is represented: the three sinir/cikis/giris forms also each expose a
*family* dataset whose extra "path" dimensions hold every axis option (subject,
topic, frequency, visitor type, geography, breakdown, direction) plus the union
of the list code lists discovered while walking the form. The preloaded
headlines generate their reports on demand and interpret the legacy Excel-HTML
grid. Any other breakdown is generated on first request through the shared
:func:`app.connectors.base.ingest_series` path.

Frequency is modelled as one dataset per frequency (``…_M``, ``…_Q``,
``…_A``) rather than a ``FREQ`` dimension: the form path and the report layout
differ per frequency (month columns vs. ``Dönem`` vs. a single year), so a
single code list per dataset is simpler and safer to validate.

The tourism income (``cikis``) and expenditure (``giris``) surveys are the old
2003–2012 visitors survey: their reports are published in thousand USD
(``(Bin $)``) with one breakdown variable (``Turizm Değişkenleri``) per report,
so they only cover those years and are interpreted as
``VARIABLE`` × ``KATEGORI`` series with a stored unit and scale.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
    ConnectorError,
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    FetchResult,
    ObjectStore,
    SourceConnector,
    upsert_discovered_codes,
)
from app.connectors.tuik.zk import (
    LegacyZkClient,
    ListItem,
    ReportGrid,
    parse_report_grid,
    parse_turkish_number,
)

logger = logging.getLogger(__name__)

CHANNEL = "turizmapp"
INSTITUTION = "tuik"
INSTITUTION_NAME = "Türkiye İstatistik Kurumu"
SOURCE_CATEGORY = "Turizm İstatistikleri"

TOTAL_CODE = "_T"
TOTAL_LABEL = "Toplam"

PAGES = {"cikis": "cikis.zul", "giris": "giris.zul", "sinir": "sinir.zul"}

# Report column labels that are a period, not a dimension value.
MONTHS: dict[str, int] = {
    "Ocak": 1,
    "Şubat": 2,
    "Mart": 3,
    "Nisan": 4,
    "Mayıs": 5,
    "Haziran": 6,
    "Temmuz": 7,
    "Ağustos": 8,
    "Agustos": 8,
    "Eylül": 9,
    "Ekim": 10,
    "Kasım": 11,
    "Aralık": 12,
}
_TOP_HEADER = "Toplam"
_YEAR_LIST = "Yıl Seçimi"
_GATE_LISTS = ("Kapı-Yol Seçimi", "Kapı Seçimi")
_MILLIYET_LIST = "Milliyet Seçimi"
_VARIABLE_LIST = "Turizm Değişkenleri"
_COUNTRY_LIST = "Ülke Seçimi"
_PROVINCE_LIST = "İl Seçimi"
_DISTRICT_LIST = "İlçe Seçimi"
_PERIOD_LIST = "Dönem Seçimi"
_DIRECTION_LABEL = "Yön"

# Income/expenditure reports: ``1.Dönem ($)`` quarter headers and the
# ``(Bin $)`` / ``(Milyon $)`` unit rows.
_QUARTER_HEADER = re.compile(r"^\s*(\d)\s*\.\s*Dönem\b", re.IGNORECASE)
_UNIT_CELL = re.compile(r"^\s*\(([^)]+)\)\s*$")
_UNIT_SCALES = {"bin": 1_000, "milyon": 1_000_000, "milyar": 1_000_000_000}

VARIABLE_CODE = "VARIABLE"
CATEGORY_CODE = "KATEGORI"

# Aggregate labels that mean "every member of this dimension" (the report has a
# total row here); they become the ``_T`` code.
_TOTAL_LABELS = {
    "_ANY": {
        "Türkiye",
        "Genel Toplam",
        "İl toplamı",
        "Milliyet Toplam",
        "Toplam",
        "Toplam ($)",
        "Toplam harcama ($)",
    },
}


@dataclass(frozen=True)
class DimensionPlan:
    """One report dimension: how its codes come from the form lists."""

    code: str
    label: str
    role: str
    source: str  # "milliyet" | "gate" | "direction"
    carry: bool = False  # blank cells inherit the value from the row above


@dataclass(frozen=True)
class FormPath:
    """One walkable form combination producing one dataset."""

    code: str
    page: str
    radios: tuple[str, ...]
    frequency: str
    name: str
    description: str
    measures: tuple[DimensionPlan, ...]
    direction: bool = False
    required: tuple[str, ...] = ()  # list headers that must be present
    measure_field: str = ""
    variable_report: bool = False  # one report per "Turizm Değişkenleri" variable
    unit: str | None = None
    scale: int | None = None


@dataclass
class InterpretedSeries:
    codes: dict[str, str]
    points: list[tuple[date, Decimal | None]]
    title: str = ""
    dataset: str = ""
    unit: str | None = None
    scale: int | None = None
    raw_object_keys: list[str] = field(default_factory=list)


@dataclass
class ReportPlan:
    """One report to generate: a dataset path plus one year (and variable).

    Splitting generation into plans lets the loader commit and resume per report
    instead of holding a whole family in memory.
    """

    dataset_code: str
    path: FormPath
    direction_code: str | None
    variable: str | None
    year: str
    discovery: WalkResult

    @property
    def page(self) -> str:
        return self.path.page


# --- form paths -----------------------------------------------------------

_SINIR_MILLIYET = (
    DimensionPlan("MILLIYET", "Milliyet", ROLE_GEO, "milliyet", carry=True),
    DimensionPlan("IL", "İl", ROLE_GEO, "gate"),
    DimensionPlan("KAPI", "Kapı", ROLE_GEO, "gate"),
    DimensionPlan("YOL", "Yol (ulaşım şekli)", ROLE_OTHER, "gate"),
)
_SINIR_GATE = (
    DimensionPlan("IL", "İl", ROLE_GEO, "gate", carry=True),
    DimensionPlan("KAPI", "Kapı", ROLE_GEO, "gate"),
    DimensionPlan("YOL", "Yol (ulaşım şekli)", ROLE_OTHER, "gate"),
)
_SINIR_VATANDAS = (
    DimensionPlan("IL", "İl", ROLE_GEO, "gate", carry=True),
    DimensionPlan("KAPI", "Kapı", ROLE_GEO, "gate"),
    DimensionPlan("YOL", "Yol (ulaşım şekli)", ROLE_OTHER, "gate"),
)

FORM_PATHS: tuple[FormPath, ...] = (
    FormPath(
        code="TUIK_TURIZM_SINIR_MILLIYET",
        page="sinir",
        radios=("Yabancı", "Aylık", "Giriş", "Milliyet-Kapı seçimi"),
        frequency="monthly",
        name="TÜİK Turizm — Aylık giriş yapan yabancı ziyaretçi (milliyet)",
        description=(
            "Sınırdan aylık giriş yapan yabancı ziyaretçi sayısı; milliyet, il, "
            "kapı ve ulaşım şekli kırılımıyla (turizmapp, eski ZK)."
        ),
        measures=_SINIR_MILLIYET,
    ),
    FormPath(
        code="TUIK_TURIZM_SINIR_KAPI",
        page="sinir",
        radios=("Yabancı", "Aylık", "Giriş", "Kapı seçimi"),
        frequency="monthly",
        name="TÜİK Turizm — Aylık giriş yapan yabancı ziyaretçi (kapı)",
        description=(
            "Sınırdan aylık giriş yapan yabancı ziyaretçi sayısı; il, kapı ve "
            "ulaşım şekli kırılımıyla (turizmapp, eski ZK)."
        ),
        measures=_SINIR_GATE,
    ),
    FormPath(
        code="TUIK_TURIZM_SINIR_VATANDAS",
        page="sinir",
        radios=("Vatandaş", "Aylık", "Giriş"),
        frequency="monthly",
        name="TÜİK Turizm — Aylık giriş/çıkış yapan vatandaş",
        description=(
            "Sınırdan aylık giriş ve çıkış yapan vatandaş sayısı; yön, il, kapı "
            "ve ulaşım şekli kırılımıyla (turizmapp, eski ZK)."
        ),
        measures=_SINIR_VATANDAS,
        direction=True,
    ),
)

FORM_PATH_BY_PAGE = {page: tuple(p for p in FORM_PATHS if p.page == page) for page in PAGES}

HEADLINES: dict[str, tuple[str, ...]] = {
    "sinir_nationality": ("TUIK_TURIZM_SINIR_MILLIYET",),
    "sinir_gate": ("TUIK_TURIZM_SINIR_KAPI",),
    "sinir_citizens": ("TUIK_TURIZM_SINIR_VATANDAS",),
    "cikis_income": ("TUIK_TURIZM_CIKIS_GELIR_Q", "TUIK_TURIZM_CIKIS_GELIR_A"),
    "giris_expenditure": ("TUIK_TURIZM_GIRIS_GIDER_Q", "TUIK_TURIZM_GIRIS_GIDER_A"),
}

# Income/expenditure reports publish one breakdown variable per report
# (``Turizm Değişkenleri``): the ``VARIABLE`` dimension names it and the report
# rows are its categories (``KATEGORI``), in thousand USD.
_REPORT_PLANS = (
    DimensionPlan(VARIABLE_CODE, "Turizm değişkeni", ROLE_OTHER, "variable"),
    DimensionPlan(CATEGORY_CODE, "Kırılım", ROLE_OTHER, "report"),
)
_VARIABLE_PATHS: tuple[FormPath, ...] = (
    FormPath(
        code="TUIK_TURIZM_CIKIS_GELIR_Q",
        page="cikis",
        radios=("Toplam(Yabancı + Vatandaş)", "Harcama", "Dönemlik", "Turizm geliri"),
        frequency="quarterly",
        name="TÜİK Turizm — Turizm geliri (dönemlik)",
        description=(
            "Yabancı ve yurtdışında ikamet eden vatandaşların çeyrek dönem turizm "
            "geliri; her 'Turizm Değişkenleri' kırılımı için, bin ABD doları "
            "(turizmapp, eski ziyaretçi anketi: yalnız 2006–2012)."
        ),
        measures=_REPORT_PLANS,
        variable_report=True,
        unit="Bin $",
        scale=1_000,
    ),
    FormPath(
        code="TUIK_TURIZM_CIKIS_GELIR_A",
        page="cikis",
        radios=("Toplam(Yabancı + Vatandaş)", "Harcama", "Yıllık", "Turizm geliri"),
        frequency="annual",
        name="TÜİK Turizm — Turizm geliri (yıllık)",
        description=(
            "Yabancı ve yurtdışında ikamet eden vatandaşların yıllık turizm "
            "geliri; her 'Turizm Değişkenleri' kırılımı için, bin ABD doları "
            "(turizmapp, eski ziyaretçi anketi: yalnız 2006–2012)."
        ),
        measures=_REPORT_PLANS,
        variable_report=True,
        unit="Bin $",
        scale=1_000,
    ),
    FormPath(
        code="TUIK_TURIZM_GIRIS_GIDER_Q",
        page="giris",
        radios=("Harcama", "Dönemlik", "Turizm gideri"),
        frequency="quarterly",
        name="TÜİK Turizm — Turizm gideri (dönemlik)",
        description=(
            "Yurtdışına çıkan vatandaşların çeyrek dönem turizm gideri; her "
            "'Turizm Değişkenleri' kırılımı için, bin ABD doları (turizmapp, "
            "eski ziyaretçi anketi: 2003–2012)."
        ),
        measures=_REPORT_PLANS,
        variable_report=True,
        unit="Bin $",
        scale=1_000,
    ),
    FormPath(
        code="TUIK_TURIZM_GIRIS_GIDER_A",
        page="giris",
        radios=("Harcama", "Yıllık", "Turizm gideri"),
        frequency="annual",
        name="TÜİK Turizm — Turizm gideri (yıllık)",
        description=(
            "Yurtdışına çıkan vatandaşların yıllık turizm gideri; her 'Turizm "
            "Değişkenleri' kırılımı için, bin ABD doları (turizmapp, eski "
            "ziyaretçi anketi: 2003–2012)."
        ),
        measures=_REPORT_PLANS,
        variable_report=True,
        unit="Bin $",
        scale=1_000,
    ),
)


FORM_PATH_BY_CODE = {path.code: path for path in (*FORM_PATHS, *_VARIABLE_PATHS)}
_INTERPRETED_CODES = frozenset(path.code for path in (*FORM_PATHS, *_VARIABLE_PATHS))


# --- full-catalog families ------------------------------------------------


@dataclass(frozen=True)
class FormAxis:
    """One radio group of a form: its axis code, label and option labels."""

    code: str
    label: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class CatalogFamily:
    """Catalog-only dataset representing every reachable radio combination.

    The user decision is to reuse one *family* dataset per page instead of
    emitting one dataset per combination (three forms × up to 324 paths). The
    family's extra path dimensions carry every axis option and
    ``attributes['path_count']`` the size of the cross-product; the value
    dimensions carry the union of the list code lists discovered while walking
    the form (lists only, no reports). This keeps the catalog complete without
    one live walk per combination.
    """

    code: str
    page: str
    name: str
    description: str
    axes: tuple[FormAxis, ...]
    explorations: tuple[tuple[str, ...], ...]

    @property
    def path_count(self) -> int:
        total = 1
        for axis in self.axes:
            total *= len(axis.options)
        return total


_CATALOG_VALUE_DIMENSIONS: dict[str, tuple[str, str]] = {
    "MILLIYET": ("Milliyet", ROLE_GEO),
    "ULKE": ("Ülke", ROLE_GEO),
    "IL": ("İl", ROLE_GEO),
    "ILCE": ("İlçe", ROLE_GEO),
    "KAPI": ("Kapı", ROLE_GEO),
    "YOL": ("Yol (ulaşım şekli)", ROLE_OTHER),
    "VARIABLE": ("Turizm değişkeni", ROLE_OTHER),
    "DONEM": ("Dönem seçimi", ROLE_OTHER),
}
_CATALOG_VALUE_ORDER = ("MILLIYET", "ULKE", "IL", "ILCE", "KAPI", "YOL", "VARIABLE", "DONEM")

_SINIR_FAMILY = CatalogFamily(
    code="TUIK_TURIZM_SINIR_CATALOG",
    page="sinir",
    name="TÜİK Turizm — Sınır formu (tüm kombinasyonlar)",
    description=(
        "Sınırdan giriş/çıkış yapan yabancı ve vatandaş sayısının bütün form "
        "kombinasyonları (kapsam × frekans × yön × kırılım) ve listelerden "
        "derlenen ortak kod listeleri (turizmapp, eski ZK; yalnız katalog)."
    ),
    axes=(
        FormAxis("KAPSAM", "Kapsam", ("Yabancı", "Vatandaş")),
        FormAxis("FREKANS", "Frekans", ("Aylık", "Dönemlik", "Yıllık")),
        FormAxis("YON", "Yön", ("Giriş", "Çıkış")),
        FormAxis(
            "KIRILIM",
            "Kırılım",
            (
                "Kapı seçimi",
                "Kapı-Milliyet seçimi",
                "Milliyet-Kapı seçimi",
                "Ülke grupları seçimi",
            ),
        ),
    ),
    explorations=(
        ("Yabancı", "Aylık", "Giriş", "Kapı seçimi"),
        ("Yabancı", "Aylık", "Giriş", "Kapı-Milliyet seçimi"),
        ("Yabancı", "Aylık", "Giriş", "Milliyet-Kapı seçimi"),
        ("Yabancı", "Aylık", "Giriş", "Ülke grupları seçimi"),
        ("Vatandaş", "Aylık", "Giriş"),
        ("Vatandaş", "Dönemlik", "Çıkış"),
        ("Yabancı", "Yıllık", "Giriş", "Milliyet-Kapı seçimi"),
    ),
)

_CIKIS_FAMILY = CatalogFamily(
    code="TUIK_TURIZM_CIKIS_CATALOG",
    page="cikis",
    name="TÜİK Turizm — Çıkış formu (tüm kombinasyonlar)",
    description=(
        "Yabancı/vatandaş çıkış ziyaretçi profil, konaklama, harcama ve turizm "
        "geliri formunun bütün kombinasyonları (kapsam × konu × frekans × "
        "ziyaretçi türü × coğrafya) ve listelerden derlenen ortak kod listeleri "
        "(turizmapp, eski ZK; yalnız katalog)."
    ),
    axes=(
        FormAxis("KAPSAM", "Kapsam", ("Yabancı", "Vatandaş", "Toplam(Yabancı + Vatandaş)")),
        FormAxis("KONU", "Konu", ("Profil", "Konaklama", "Harcama")),
        FormAxis("FREKANS", "Frekans", ("Aylık", "Dönemlik", "Yıllık")),
        FormAxis("ZIYARETCI", "Ziyaretçi türü", ("Bireysel", "Paket", "Turizm geliri")),
        FormAxis(
            "COGRAFI",
            "Coğrafya",
            ("Türkiye", "İl bazında", "İlce bazında", "Belde bazında"),
        ),
    ),
    explorations=(
        ("Yabancı", "Profil", "Aylık", "Bireysel", "Türkiye"),
        ("Yabancı", "Profil", "Dönemlik", "Bireysel", "İl bazında"),
        ("Yabancı", "Profil", "Yıllık", "Bireysel", "İlce bazında"),
        ("Yabancı", "Profil", "Aylık", "Bireysel", "Belde bazında"),
        ("Yabancı", "Konaklama", "Aylık", "Bireysel"),
        ("Vatandaş", "Konaklama", "Dönemlik", "Bireysel"),
        ("Toplam(Yabancı + Vatandaş)", "Harcama", "Aylık", "Bireysel"),
        ("Toplam(Yabancı + Vatandaş)", "Harcama", "Dönemlik", "Turizm geliri"),
        ("Toplam(Yabancı + Vatandaş)", "Harcama", "Yıllık", "Turizm geliri"),
        ("Yabancı", "Harcama", "Aylık", "Paket"),
    ),
)

_GIRIS_FAMILY = CatalogFamily(
    code="TUIK_TURIZM_GIRIS_CATALOG",
    page="giris",
    name="TÜİK Turizm — Giriş formu (tüm kombinasyonlar)",
    description=(
        "Yurtdışından dönen vatandaş profil, konaklama, harcama ve turizm "
        "gideri formunun bütün kombinasyonları (konu × frekans × ziyaretçi "
        "türü) ve listelerden derlenen ortak kod listeleri (turizmapp, eski ZK; "
        "yalnız katalog)."
    ),
    axes=(
        FormAxis("KONU", "Konu", ("Profil", "Konaklama", "Harcama")),
        FormAxis("FREKANS", "Frekans", ("Aylık", "Dönemlik", "Yıllık")),
        FormAxis("ZIYARETCI", "Ziyaretçi türü", ("Bireysel", "Paket", "Turizm gideri")),
    ),
    explorations=(
        ("Profil", "Aylık", "Bireysel"),
        ("Konaklama", "Dönemlik", "Bireysel"),
        ("Harcama", "Aylık", "Bireysel"),
        ("Harcama", "Dönemlik", "Turizm gideri"),
        ("Harcama", "Yıllık", "Turizm gideri"),
    ),
)

CATALOG_FAMILIES: tuple[CatalogFamily, ...] = (
    _SINIR_FAMILY,
    _CIKIS_FAMILY,
    _GIRIS_FAMILY,
)
CATALOG_FAMILY_BY_CODE = {family.code: family for family in CATALOG_FAMILIES}


def walk_family(client: LegacyZkClient, family: CatalogFamily) -> WalkResult:
    """Walk every exploration path of a family (lists only) and merge the codes."""
    result = WalkResult()
    for radios in family.explorations:
        path = FormPath(
            code=family.code,
            page=family.page,
            radios=radios,
            frequency="monthly",
            name=family.name,
            description=family.description,
            measures=(),
        )
        try:
            part = walk_form(client, path, generate=False)
        except ConnectorError:
            logger.warning("%s: exploration %s failed", family.code, radios, exc_info=True)
            continue
        result.codes = _merge_code_lists(result.codes, part.codes)
        for dimension, labels in part.labels.items():
            result.labels.setdefault(dimension, {}).update(labels)
        for year in part.years:
            if year not in result.years:
                result.years.append(year)
        result.raw_keys.extend(part.raw_keys)
    return result


# --- form walking ---------------------------------------------------------


@dataclass
class WalkResult:
    """Code lists and (optionally) one report collected from a form path."""

    codes: dict[str, dict[str, str]] = field(default_factory=dict)  # dim -> {value: code}
    labels: dict[str, dict[str, str]] = field(default_factory=dict)  # code -> label
    years: list[str] = field(default_factory=list)
    grid: ReportGrid | None = None
    variable: str | None = None
    detail: str = ""
    raw_keys: list[str] = field(default_factory=list)


def _select_ids(items: Sequence[ListItem], *, year_values: set[str] | None) -> list[str]:
    return [item.id for item in items if year_values is None or item.label in year_values]


def _register_list_items(result: WalkResult, header: str, items: Sequence[ListItem]) -> None:
    """Merge one listbox's items into the run's code lists (by header kind)."""
    if header == _YEAR_LIST:
        for item in items:
            if item.label not in result.years:
                result.years.append(item.label)
        return
    milliyet = header.startswith(_MILLIYET_LIST)
    gate = any(header.startswith(name) for name in _GATE_LISTS)
    variable = header.startswith(_VARIABLE_LIST)
    country = header.startswith(_COUNTRY_LIST)
    province = header.startswith(_PROVINCE_LIST)
    district = header.startswith(_DISTRICT_LIST)
    period = header.startswith(_PERIOD_LIST)
    if not (milliyet or gate or variable or country or province or district or period):
        return
    for item in items:
        if item.label.startswith("<<"):
            continue
        if milliyet:
            _add_code(result, "MILLIYET", item.label, item.label)
            continue
        if variable:
            _add_code(result, VARIABLE_CODE, item.label, item.label)
            continue
        if country:
            _add_code(result, "ULKE", item.label, item.label)
            continue
        if province:
            _add_code(result, "IL", item.label, item.label)
            continue
        if district:
            _add_code(result, "ILCE", item.label, item.label)
            continue
        if period:
            _add_code(result, "DONEM", item.label, item.label)
            continue
        province_name, gate_name, road = split_gate_label(item.label)
        if province_name:
            _add_code(result, "IL", province_name, province_name)
        if gate_name:
            _add_code(result, "KAPI", gate_name, gate_name)
        if road:
            _add_code(result, "YOL", road, road)


def _add_code(result: WalkResult, dimension: str, value: str, label: str) -> None:
    result.codes.setdefault(dimension, {})[value] = value
    result.labels.setdefault(dimension, {})[value] = label


def _add_total(result: WalkResult, dimension: str) -> None:
    payload = result.codes.setdefault(dimension, {})
    payload[TOTAL_CODE] = TOTAL_CODE
    result.labels.setdefault(dimension, {})[TOTAL_CODE] = TOTAL_LABEL


def walk_form(
    client: LegacyZkClient,
    path: FormPath,
    *,
    year_values: set[str] | None = None,
    variable: str | None = None,
    generate: bool = True,
    max_rounds: int = 12,
) -> WalkResult:
    """Drive one form path with list selections only, optionally generating it.

    ``variable`` restricts the ``Turizm Değişkenleri`` selection to one
    breakdown variable (income/expenditure need exactly one report per variable);
    when omitted every visible list item is selected.
    """
    client.bootstrap()
    result = WalkResult(variable=variable)
    for label in path.radios:
        client.check_radio(label)
    checked: set[str] = set()
    handled: set[str] = set()
    year_seen = False
    year_selected = False
    for _ in range(max_rounds):
        changed = False
        for label in client.visible_checkboxes():
            if label in checked:
                continue
            client.check_box(label)
            checked.add(label)
            changed = True
        for box_id in client.visible_listboxes():
            header = client.list_header(box_id)
            items = client.list_items(box_id)
            if not items or box_id in handled:
                continue
            _register_list_items(result, header, items)
            if header == _YEAR_LIST:
                year_seen = True
                ids = _select_ids(items, year_values=year_values)
                year_selected = year_selected or bool(ids)
            elif header.startswith(_VARIABLE_LIST) and variable is not None:
                ids = [item.id for item in items if item.label == variable]
            else:
                ids = _select_ids(items, year_values=None)
            if not ids:
                handled.add(box_id)
                continue
            client.select_items(box_id, ids)
            handled.add(box_id)
            changed = True
        if not changed:
            break
    if generate and not (year_values is not None and year_seen and not year_selected):
        content, _url = client.generate_report()
        result.grid = parse_report_grid(content)
    result.raw_keys = client.take_raw_keys()
    return result


def split_gate_label(label: str) -> tuple[str | None, str | None, str | None]:
    """Split a ``Kapı-Yol`` list item ``"Adana - Botaş (Denizyolu)"``."""
    value = label.strip()
    road: str | None = None
    if value.endswith(")") and "(" in value:
        value, _sep, tail = value.rpartition("(")
        road = tail[:-1].strip() or None
        value = value.strip()
    province: str | None = None
    gate: str | None = None
    if " - " in value:
        province, _sep, gate = value.partition(" - ")
        province = province.strip() or None
        gate = gate.strip() or None
    else:
        gate = value or None
    return province, gate, road


def normalize_code_value(value: str) -> str:
    """Normalise a report cell to a catalogue code value.

    Detailed gate cells concatenate the province and the gate with a comma
    (``"Adana,Şakirpaşa"`` while the list item is ``"Adana - Şakirpaşa"``), so
    the gate code is the part after the last comma.
    """
    text = value.strip()
    if "," in text:
        text = text.rpartition(",")[2].strip()
    return text


# --- report interpretation ------------------------------------------------


def find_period_header(grid: ReportGrid) -> tuple[int, dict[int, str], int | None] | None:
    """Locate the header row with month names and the report year above it."""
    header_index = -1
    best = 0
    for index, row in enumerate(grid.rows):
        count = sum(1 for cell in row if cell in MONTHS)
        if count > best:
            best = count
            header_index = index
    if header_index < 0 or best < 4:
        return None
    periods = {
        column: MONTHS[cell]
        for column, cell in enumerate(grid.rows[header_index])
        if cell in MONTHS
    }
    year: int | None = None
    for row in grid.rows[:header_index]:
        for cell in row:
            if len(cell) == 4 and cell.isdigit() and 1900 <= int(cell) <= 2200:
                year = int(cell)
    return header_index, periods, year


def _leading_columns(grid: ReportGrid, header_index: int, period_columns: set[int]) -> int:
    first = min(period_columns) if period_columns else grid.width
    return first


def _dimension_code(
    plan: DimensionPlan,
    raw: str,
    *,
    carry_value: str | None,
    valid: dict[str, str],
) -> str | None:
    text = raw.strip()
    if not text:
        if plan.carry and carry_value:
            return carry_value
        return TOTAL_CODE
    if text in _TOTAL_LABELS["_ANY"]:
        return TOTAL_CODE
    normalized = normalize_code_value(text)
    if normalized in valid:
        return normalized
    if text in valid:
        return text
    return None


def interpret_monthly(
    grid: ReportGrid,
    plans: Sequence[DimensionPlan],
    valid: dict[str, dict[str, str]],
    *,
    direction_code: str | None = None,
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret a monthly "label columns + month columns" report.

    Leading text columns become dimensions (blank cells become ``_T`` or, for a
    ``carry`` column, inherit the row above); the month columns become the
    period points. The annual ``Toplam`` column is skipped for a monthly
    report (the twelve months already carry the year). Rows whose dimension
    value is not a catalogued code are reported, not silently mapped.
    """
    header = find_period_header(grid)
    if header is None:
        raise ConnectorError(FORMAT_CHANGED, "report has no month header row")
    header_index, periods, year = header
    if year is None:
        raise ConnectorError(FORMAT_CHANGED, "report has no year above the month header")
    first_period = min(periods)
    if first_period < len(plans):
        raise ConnectorError(
            FORMAT_CHANGED, f"report has {first_period} leading columns, need {len(plans)}"
        )
    last_carry: dict[int, str] = {}
    series: dict[tuple[str, ...], InterpretedSeries] = {}
    uninterpreted: list[str] = []
    for row in grid.rows[header_index + 1 :]:
        month_values = {
            column: parse_turkish_number(row[column]) if column < len(row) else None
            for column in periods
        }
        if all(value is None for value in month_values.values()):
            continue
        codes: list[str] = []
        ok = True
        for position, plan in enumerate(plans):
            raw = row[position] if position < len(row) else ""
            code = _dimension_code(
                plan, raw, carry_value=last_carry.get(position), valid=valid.get(plan.code, {})
            )
            if code is None:
                uninterpreted.append(raw.strip())
                ok = False
                break
            if plan.carry and raw.strip():
                last_carry[position] = code
            codes.append(code)
        if not ok:
            continue
        if direction_code is not None:
            codes = [direction_code, *codes]
        points: list[tuple[date, Decimal | None]] = []
        for column, month in periods.items():
            points.append((date(year, month, 1), month_values.get(column)))
        key = tuple(codes)
        existing = series.get(key)
        if existing is None:
            series[key] = InterpretedSeries(
                codes=dict(zip(_plan_codes(plans, direction_code), codes, strict=True)),
                points=points,
            )
        else:
            existing.points.extend(points)
    for entry in series.values():
        entry.points = _sorted_points(entry.points)
    return list(series.values()), uninterpreted


def _sorted_points(
    points: Sequence[tuple[date, Decimal | None]],
) -> list[tuple[date, Decimal | None]]:
    """Sort by period, keeping the first value per date (never comparing values).

    Two report rows can normalise to the same code (e.g. a grand-total row and a
    "Türkiye" row); their points must be merged without sorting ``Decimal``
    against ``None``.
    """
    by_date: dict[date, Decimal | None] = {}
    for period, value in points:
        if period not in by_date or by_date[period] is None:
            by_date[period] = value
    return sorted(by_date.items())


def _plan_codes(plans: Sequence[DimensionPlan], direction_code: str | None) -> list[str]:
    codes = [plan.code for plan in plans]
    if direction_code is not None:
        codes = ["DIRECTION", *codes]
    return codes


# --- income/expenditure reports -------------------------------------------


@dataclass(frozen=True)
class VariableReport:
    """The period/year/unit layout of an income or expenditure report."""

    header_index: int
    periods: dict[int, int | None]  # column -> quarter (1-4) or None for annual
    year: int
    unit: str | None
    scale: int | None
    category_column: int = 0


def parse_unit(text: str) -> tuple[str, int] | None:
    """Read a ``(Bin $)`` / ``(Milyon $)`` unit cell into ``(unit, scale)``."""
    match = _UNIT_CELL.match(text or "")
    if not match:
        return None
    unit = match.group(1).strip()
    if not unit or "$" not in unit and "₺" not in unit:
        return None
    lowered = unit.lower()
    for token, factor in _UNIT_SCALES.items():
        if lowered.startswith(token):
            return unit, factor
    return unit, 1


def find_variable_header(grid: ReportGrid, frequency: str) -> VariableReport | None:
    """Locate the quarter/value header of an income/expenditure report.

    Quarterly reports have a ``1.Dönem ($) … 4.Dönem ($)`` header row and a year
    row above it; annual reports use a single ``Toplam ($)`` value column with
    the year either above the header or in the header's first cell.
    """
    quarterly = _quarterly_report(grid)
    if frequency == "quarterly" or quarterly is not None:
        if quarterly is None:
            return None
        header_index, periods = quarterly
    else:
        annual = _annual_report(grid)
        if annual is None:
            return None
        header_index, periods = annual

    year: int | None = None
    unit: str | None = None
    scale: int | None = None
    for row in grid.rows[: header_index + 1]:
        for cell in row:
            if year is None and len(cell) == 4 and cell.isdigit() and 1900 <= int(cell) <= 2200:
                year = int(cell)
            if unit is None:
                parsed = parse_unit(cell)
                if parsed is not None:
                    unit, scale = parsed
    if year is None:
        return None
    return VariableReport(
        header_index=header_index, periods=periods, year=year, unit=unit, scale=scale
    )


def _quarterly_report(grid: ReportGrid) -> tuple[int, dict[int, int]] | None:
    best: tuple[int, dict[int, int]] | None = None
    for index, row in enumerate(grid.rows):
        periods = {
            column: int(match.group(1))
            for column, cell in enumerate(row)
            if (match := _QUARTER_HEADER.match(cell)) is not None
        }
        if len(periods) >= 3 and (best is None or len(periods) > len(best[1])):
            best = (index, periods)
    return best


def _annual_report(grid: ReportGrid) -> tuple[int, dict[int, None]] | None:
    for index, row in enumerate(grid.rows):
        columns = {
            column: None
            for column, cell in enumerate(row)
            if cell.startswith("Toplam") and "(" in cell and column > 0
        }
        if columns:
            return index, columns
    return None


def _category_code(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    if text in _TOTAL_LABELS["_ANY"]:
        return TOTAL_CODE
    return normalize_code_value(text)


def interpret_variable_report(
    grid: ReportGrid,
    *,
    variable: str | None,
    frequency: str,
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret one income/expenditure report (one breakdown variable).

    The first column carries the variable's categories and the value columns are
    the quarters (or the single annual column); blank/notes rows are skipped.
    The unit and scale (``Bin $`` → 1000) are read from the report and attached
    to every series.
    """
    report = find_variable_header(grid, frequency)
    if report is None:
        raise ConnectorError(FORMAT_CHANGED, "report has no income/expenditure header row")
    if variable is None:
        raise ConnectorError(FORMAT_CHANGED, "income/expenditure report needs a variable")
    series: dict[str, InterpretedSeries] = {}
    uninterpreted: list[str] = []
    last_category: str | None = None
    for row in grid.rows[report.header_index + 1 :]:
        raw = row[report.category_column] if row else ""
        if raw.strip():
            last_category = raw
        values = {
            column: parse_turkish_number(row[column]) if column < len(row) else None
            for column in report.periods
        }
        if all(value is None for value in values.values()):
            continue
        if not raw.strip():
            # The label and its values can be split across two physical rows: a
            # ``rowspan`` label row (no numbers, skipped above) followed by a
            # values-only row, or a category row followed by extra value columns.
            # Carry the previous category; a leading blank row is a spacer.
            if last_category is None:
                logger.debug(
                    "%s: dropped a report row with an empty category and no preceding label",
                    variable,
                )
                continue
            raw = last_category
        code = _category_code(raw)
        if code is None:
            uninterpreted.append(raw)
            continue
        points: list[tuple[date, Decimal | None]] = []
        for column, quarter in report.periods.items():
            month = 1 if quarter is None else (quarter - 1) * 3 + 1
            points.append((date(report.year, month, 1), values.get(column)))
        entry = series.get(code)
        if entry is None:
            series[code] = InterpretedSeries(
                codes={VARIABLE_CODE: variable, CATEGORY_CODE: code},
                points=points,
                unit=report.unit,
                scale=report.scale,
            )
        else:
            entry.points.extend(points)
    for entry in series.values():
        entry.points = _sorted_points(entry.points)
    return list(series.values()), uninterpreted


def distinct_series_count(series: Sequence[InterpretedSeries]) -> int:
    """Number of distinct series across per-year/per-variable chunks.

    Two chunks belong to the same series when they share the dataset and every
    dimension code; only their observation points differ (one report per year).
    """
    return len({(entry.dataset, tuple(sorted(entry.codes.items()))) for entry in series})


# --- connector ------------------------------------------------------------


class TurizmConnector(SourceConnector):
    """Lists the tourism datasets and generates/loads their series."""

    institution_code = INSTITUTION
    institution_name = INSTITUTION_NAME
    channel = CHANNEL

    def __init__(
        self,
        *,
        client_factory: Any = None,
        store: ObjectStore | None = None,
        settings_obj: Settings | None = None,
    ) -> None:
        self._settings = settings_obj or global_settings
        self._store = store
        self._client_factory = client_factory or self._default_client

    def _default_client(self, page: str, dataset: str) -> LegacyZkClient:
        return LegacyZkClient(
            PAGES[page],
            settings_obj=self._settings,
            store=self._store,
            institution=INSTITUTION,
            dataset=dataset,
        )

    def _client(self, page: str, dataset: str) -> LegacyZkClient:
        return self._client_factory(page, dataset)

    # -- catalog -----------------------------------------------------------

    def list_datasets(self) -> Iterator[DatasetMeta]:
        for path in (*FORM_PATHS, *_VARIABLE_PATHS):
            client = self._client(path.page, path.code)
            try:
                result = walk_form(client, path, generate=False)
            finally:
                client.close()
            yield self._dataset_meta(path, result)
        for family in CATALOG_FAMILIES:
            client = self._client(family.page, family.code)
            try:
                result = walk_family(client, family)
            finally:
                client.close()
            yield self._family_meta(family, result)

    def _dataset_meta(
        self,
        path: FormPath,
        result: WalkResult,
        *,
        extra_codes: dict[str, dict[str, str]] | None = None,
    ) -> DatasetMeta:
        dimensions: list[DimensionMeta] = []
        offset = 0
        if path.direction:
            dimensions.append(
                DimensionMeta(
                    code="DIRECTION",
                    label="Yön",
                    position=0,
                    role=ROLE_OTHER,
                    codes=[
                        DimensionCodeMeta(code="GIRIS", label="Giriş"),
                        DimensionCodeMeta(code="CIKIS", label="Çıkış"),
                    ],
                )
            )
            offset = 1
        for position, plan in enumerate(path.measures):
            known = dict(result.codes.get(plan.code, {}))
            known.update((extra_codes or {}).get(plan.code, {}))
            codes = [
                DimensionCodeMeta(code=code, label=result.labels.get(plan.code, {}).get(code, code))
                for code in sorted(known)
                if code != TOTAL_CODE
            ]
            codes.insert(0, DimensionCodeMeta(code=TOTAL_CODE, label=TOTAL_LABEL, is_default=True))
            dimensions.append(
                DimensionMeta(
                    code=plan.code,
                    label=plan.label,
                    position=position + offset,
                    role=plan.role,
                    codes=codes,
                    attributes={"source": plan.source},
                )
            )
        dimensions.append(
            DimensionMeta(
                code="TIME_PERIOD",
                label="Dönem",
                position=len(path.measures) + offset,
                role=ROLE_TIME,
                codes=[],
                attributes={"frequency": path.frequency},
            )
        )
        coverage_start, coverage_end = _coverage(result.years, path.frequency)
        attributes: dict[str, Any] = {
            "channel": CHANNEL,
            "page": path.page,
            "radios": list(path.radios),
            "default_frequency": path.frequency,
            "measures": [plan.code for plan in path.measures],
            "interpreted": path.code in _INTERPRETED_CODES,
        }
        if path.variable_report:
            attributes["variable_report"] = True
        if path.unit is not None:
            attributes["unit"] = path.unit
        if path.scale is not None:
            attributes["scale"] = path.scale
        return DatasetMeta(
            external_code=path.code,
            name=path.name,
            description=path.description,
            source_category=SOURCE_CATEGORY,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            attributes=attributes,
            dimensions=dimensions,
        )

    def _family_meta(self, family: CatalogFamily, result: WalkResult) -> DatasetMeta:
        dimensions: list[DimensionMeta] = []
        position = 0
        for axis in family.axes:
            dimensions.append(
                DimensionMeta(
                    code=axis.code,
                    label=axis.label,
                    position=position,
                    role=ROLE_OTHER,
                    codes=[DimensionCodeMeta(code=option, label=option) for option in axis.options],
                    attributes={"path_dimension": True},
                )
            )
            position += 1
        for code in _CATALOG_VALUE_ORDER:
            values = result.codes.get(code)
            if not values:
                continue
            label, role = _CATALOG_VALUE_DIMENSIONS[code]
            codes = [
                DimensionCodeMeta(code=value, label=result.labels.get(code, {}).get(value, value))
                for value in sorted(values)
            ]
            dimensions.append(
                DimensionMeta(
                    code=code,
                    label=label,
                    position=position,
                    role=role,
                    codes=codes,
                    attributes={"source": "list"},
                )
            )
            position += 1
        dimensions.append(
            DimensionMeta(
                code="TIME_PERIOD",
                label="Dönem",
                position=position,
                role=ROLE_TIME,
                codes=[],
                attributes={"frequency": "mixed"},
            )
        )
        coverage_start, coverage_end = _coverage(result.years, "monthly")
        return DatasetMeta(
            external_code=family.code,
            name=family.name,
            description=family.description,
            source_category=SOURCE_CATEGORY,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            attributes={
                "channel": CHANNEL,
                "page": family.page,
                "radios": [],
                "default_frequency": "mixed",
                "measures": [],
                "interpreted": False,
                "catalog_family": True,
                "path_count": family.path_count,
                "path_dimensions": {axis.code: list(axis.options) for axis in family.axes},
            },
            dimensions=dimensions,
        )

    # -- fetch -------------------------------------------------------------

    def close(self) -> None:
        """No persistent connection; present so callers can close uniformly."""

    def dataset_meta(
        self,
        dataset_code: str,
        *,
        extra_codes: dict[str, dict[str, str]] | None = None,
    ) -> DatasetMeta:
        """Walk one dataset's form (no report) and build its catalogue metadata.

        ``extra_codes`` merges codes discovered elsewhere (e.g. the categories of
        an income/expenditure report generated for a headline) into the code
        lists.
        """
        path = FORM_PATH_BY_CODE.get(dataset_code)
        if path is None:
            raise ConnectorError(FORMAT_CHANGED, f"unknown dataset {dataset_code!r}")
        client = self._client(path.page, path.code)
        try:
            result = walk_form(client, path, generate=False)
        finally:
            client.close()
        return self._dataset_meta(path, result, extra_codes=extra_codes)

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        path = FORM_PATH_BY_CODE.get(dataset_code)
        if path is None:
            raise ConnectorError(FORMAT_CHANGED, f"unknown dataset {dataset_code!r}")
        discovery, direction_code, radios, wanted = self._discovery(path, codes)
        if direction_code is None and path.direction:
            raise ConnectorError(FORMAT_CHANGED, "DIRECTION is required")
        years = _years_from(start, discovery.years)
        variables = (wanted[VARIABLE_CODE],) if VARIABLE_CODE in wanted else ()
        series = self._collect_years(
            discovery, _with_radios(path, radios), years, direction_code, variables
        )
        points = _merge_selected_points(series, wanted)
        points = [(period, value) for period, value in points if period >= start]
        if not points:
            raise ConnectorError(
                EMPTY, f"{dataset_code}: no observations matched {codes} at or after {start}"
            )
        return FetchResult(
            external_code=_external_code(path, codes),
            points=points,
            raw_object_keys=[],
            channel=CHANNEL,
        )

    def register_discovered_codes(self, session: Any, dataset: Any, codes: dict[str, str]) -> None:
        """Add report-only income/expenditure categories before ``ensure_series``."""
        upsert_discovered_codes(session, dataset, codes)

    def interpret(
        self, path: FormPath, result: WalkResult, *, direction_code: str | None = None
    ) -> list[InterpretedSeries]:
        if path.code not in _INTERPRETED_CODES or result.grid is None:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{path.code}: report family is not interpretable by the generic interpreter",
            )
        if path.variable_report:
            series, uninterpreted = interpret_variable_report(
                result.grid, variable=result.variable, frequency=path.frequency
            )
        else:
            series, uninterpreted = interpret_monthly(
                result.grid,
                path.measures,
                result.codes,
                direction_code=direction_code,
            )
        if uninterpreted:
            logger.info(
                "%s: skipped %d rows with uncatalogued values (e.g. %s)",
                path.code,
                len(uninterpreted),
                uninterpreted[:5],
            )
        for entry in series:
            entry.dataset = path.code
        return series

    # -- headlines ---------------------------------------------------------

    def headline_series(self, name: str, *, years: int | None = None) -> list[InterpretedSeries]:
        """Load one preconfigured headline. ``years`` keeps the newest N years."""
        collected: list[InterpretedSeries] = []
        for plan in self.headline_plans(name, years=years):
            collected.extend(self.build_report(plan))
        return collected

    def headline_plans(self, name: str, *, years: int | None = None) -> list[ReportPlan]:
        """Enumerate the reports behind one headline, newest ``years`` first."""
        if name not in HEADLINES:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"unknown headline {name!r}; choose from {sorted(HEADLINES)}",
            )
        plans: list[ReportPlan] = []
        for code in HEADLINES[name]:
            path = FORM_PATH_BY_CODE[code]
            discovery, _direction, _radios, _wanted = self._discovery(path, None)
            selected = _newest_years(discovery.years, years)
            variables = (
                tuple(sorted(discovery.codes.get(VARIABLE_CODE, {})))
                if path.variable_report
                else ()
            )
            if path.direction:
                variants = (("GIRIS", ("Giriş",)), ("CIKIS", ("Çıkış",)))
            else:
                variants = ((None, ()),)
            for direction_code, extra in variants:
                variant = _with_radios(path, path.radios + extra)
                if path.variable_report:
                    for variable in variables:
                        available = _newest_years(self._variable_years(variant, variable), years)
                        for year in available:
                            plans.append(
                                ReportPlan(code, variant, direction_code, variable, year, discovery)
                            )
                else:
                    for year in selected:
                        plans.append(
                            ReportPlan(code, variant, direction_code, None, year, discovery)
                        )
        return plans

    def discovery_plans(self, *, page: str | None = None) -> list[ReportPlan]:
        """One newest report per variable for every report-only-catalogued family.

        Income/expenditure categories are visible only inside generated reports,
        so a light pass over the newest year of each variable fills the catalog.
        """
        plans: list[ReportPlan] = []
        for path in _VARIABLE_PATHS:
            if page is not None and path.page != page:
                continue
            discovery, _direction, _radios, _wanted = self._discovery(path, None)
            for variable in sorted(discovery.codes.get(VARIABLE_CODE, {})):
                years = _newest_years(self._variable_years(path, variable), 1)
                if not years:
                    continue
                plans.append(ReportPlan(path.code, path, None, variable, years[0], discovery))
        return plans

    def build_report(self, plan: ReportPlan) -> list[InterpretedSeries]:
        """Generate and interpret one ``ReportPlan`` (code lists merged in)."""
        client = self._client(plan.path.page, plan.dataset_code)
        try:
            result = walk_form(
                client,
                plan.path,
                year_values={plan.year},
                generate=True,
                variable=plan.variable,
            )
        finally:
            client.close()
        if result.grid is None:
            return []
        combined = WalkResult(
            codes=_merge_code_lists(plan.discovery.codes, result.codes),
            labels={**plan.discovery.labels, **result.labels},
            years=plan.discovery.years,
            grid=result.grid,
            variable=plan.variable,
            raw_keys=result.raw_keys,
        )
        chunk = self.interpret(plan.path, combined, direction_code=plan.direction_code)
        for entry in chunk:
            entry.raw_object_keys = result.raw_keys
        return chunk

    def _variable_years(self, path: FormPath, variable: str) -> list[str]:
        """Available years for one ``Turizm Değişkenleri`` breakdown variable."""
        client = self._client(path.page, path.code)
        try:
            result = walk_form(client, path, variable=variable, generate=False)
        finally:
            client.close()
        return result.years

    # -- internals ---------------------------------------------------------

    def _discovery(
        self, path: FormPath, codes: dict[str, str] | None
    ) -> tuple[WalkResult, str | None, tuple[str, ...], dict[str, str]]:
        """One report-free walk collecting the full code lists and years."""
        client = self._client(path.page, path.code)
        try:
            discovery = walk_form(client, path, generate=False)
        finally:
            client.close()
        direction_code: str | None = None
        radios = path.radios
        if path.direction and codes is not None:
            direction_code = codes.get("DIRECTION")
            if direction_code == "CIKIS":
                radios = path.radios + ("Çıkış",)
            elif direction_code == "GIRIS":
                radios = path.radios + ("Giriş",)
        wanted = (
            {plan.code: codes[plan.code] for plan in path.measures if plan.code in codes}
            if codes is not None
            else {}
        )
        return discovery, direction_code, radios, wanted

    def _collect_years(
        self,
        discovery: WalkResult,
        path: FormPath,
        years: Sequence[str],
        direction_code: str | None,
        variables: Sequence[str] = (),
    ) -> list[InterpretedSeries]:
        """Generate one report per selected year (and variable) and interpret it."""
        collected: list[InterpretedSeries] = []
        if path.variable_report:
            wanted_variables = tuple(variables) or tuple(
                sorted(discovery.codes.get(VARIABLE_CODE, {}))
            )
            selections: list[tuple[str | None, str]] = [
                (variable, year) for variable in wanted_variables for year in years
            ]
        else:
            selections = [(None, year) for year in years]
        for variable, year in selections:
            client = self._client(path.page, path.code)
            try:
                result = walk_form(
                    client, path, year_values={year}, generate=True, variable=variable
                )
            finally:
                client.close()
            if result.grid is None:
                continue
            combined = WalkResult(
                codes=_merge_code_lists(discovery.codes, result.codes),
                labels={**discovery.labels, **result.labels},
                years=discovery.years,
                grid=result.grid,
                variable=variable,
                raw_keys=result.raw_keys,
            )
            chunk = self.interpret(path, combined, direction_code=direction_code)
            for entry in chunk:
                entry.raw_object_keys = result.raw_keys
            collected.extend(chunk)
        return collected


def _with_radios(path: FormPath, radios: tuple[str, ...]) -> FormPath:
    return replace(path, radios=radios)


def _merge_code_lists(
    first: dict[str, dict[str, str]], second: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    merged = {dimension: dict(values) for dimension, values in first.items()}
    for dimension, values in second.items():
        merged.setdefault(dimension, {}).update(values)
    return merged


def _merge_selected_points(
    series: Sequence[InterpretedSeries], wanted: dict[str, str]
) -> list[tuple[date, Decimal | None]]:
    points: list[tuple[date, Decimal | None]] = []
    for entry in series:
        if all(entry.codes.get(dimension) == code for dimension, code in wanted.items()):
            points.extend(entry.points)
    return _sorted_points(points)


def _external_code(path: FormPath, codes: dict[str, str]) -> str:
    ordered: list[str] = []
    if path.direction and "DIRECTION" in codes:
        ordered.append(codes["DIRECTION"])
    ordered.extend(codes[plan.code] for plan in path.measures if plan.code in codes)
    return f"{path.code}:" + ".".join(ordered)


def _newest_years(years: Sequence[str], count: int | None) -> list[str]:
    ordered = sorted({year for year in years if year.isdigit()}, reverse=True)
    if count is None:
        return ordered
    return ordered[: max(1, count)]


def _years_from(start: date, years: Sequence[str]) -> list[str]:
    ordered = sorted({year for year in years if year.isdigit()}, reverse=True)
    return [year for year in ordered if int(year) >= start.year]


def _coverage(years: Sequence[str], frequency: str) -> tuple[date | None, date | None]:
    values = sorted(int(year) for year in years if year.isdigit())
    if not values:
        return None, None
    end_month = 12 if frequency == "monthly" else (10 if frequency == "quarterly" else 1)
    return date(values[0], 1, 1), date(values[-1], end_month, 1)


__all__ = [
    "CATALOG_FAMILIES",
    "CATALOG_FAMILY_BY_CODE",
    "CATEGORY_CODE",
    "CHANNEL",
    "FORM_PATHS",
    "FORM_PATH_BY_CODE",
    "HEADLINES",
    "INSTITUTION",
    "VARIABLE_CODE",
    "CatalogFamily",
    "DimensionPlan",
    "FormAxis",
    "FormPath",
    "InterpretedSeries",
    "LegacyZkClient",
    "MONTHS",
    "PAGES",
    "TOTAL_CODE",
    "ReportPlan",
    "TurizmConnector",
    "VariableReport",
    "WalkResult",
    "distinct_series_count",
    "find_period_header",
    "find_variable_header",
    "interpret_monthly",
    "interpret_variable_report",
    "normalize_code_value",
    "parse_report_grid",
    "parse_turkish_number",
    "parse_unit",
    "split_gate_label",
    "walk_family",
    "walk_form",
]
