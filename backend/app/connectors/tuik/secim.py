"""TÜİK election results (``biruni.tuik.gov.tr/secimdagitimapp``) connector.

``secimdagitimapp`` is a legacy ZK "DHTML" application (the same old AU dialect
as ``turizmapp``) exposing ten tables of the Turkish parliamentary election
results, from the national summary down to the ballot-box level.

The page's first listbox ("Tablo seçimi") is filled by the server *in the
bootstrap HTML* and holds the ten tables; selecting one reveals its inputs:

- ``Seçim yılına göre …`` (by year): a year-range radio, then a province and a
  party — the report is a wide matrix of elections (columns) × metric/party
  (rows), with ``A`` votes, ``B`` shares and ``C`` seats.
- ``Seçim çevresi ve ilçelere/bölgelerine/sandık kurullarına göre …``: a year
  radio (or a "Yıllar" list), then a cascade of geography lists down to the
  settlement/ballot region.
- ``Milletvekili genel seçimine katılan adaylar``: candidates by province and
  party, with an elected flag.
- ``Yıllara ve illere göre … milletvekili sayısı``: party seat counts.
- ``Gümrük kapıları`` / ``Yurt dışı sandıkları``: customs gates and countries.
- ``Milletvekili genel seçimi sonucu``: the national summary.

Model
-----
Each table becomes one dataset (``TUIK_SECIM_…``). The election period is the
real date of the selected election, resolved from the table's own label (a
report title date is only a cross-check); an unresolved election fails rather
than inventing a date. The frequency is ``irregular``. A report cell is
modelled as four kinds of code:

- ``TIME_PERIOD`` — the election date,
- the geography dimensions the form lists expose (province/constituency,
  district, settlement, ballot region, customs province, country, consulate),
- ``PARTI`` — the party name (a report header or row; discovered per table and
  election, because it differs every election),
- ``MEASURE`` — ``VOTES`` (mutlak sonuç), ``VOTE_SHARE`` (oransal sonuç) and the
  fixed count columns (sandık sayısı, kayıtlı seçmen, oy kullanan, geçerli oy,
  katılım oranı, milletvekili sayısı).

The catalog walks the form lists (no reports) and stores every code the lists
expose plus the discovered target for the party codes; a light discovery pass
generates the smallest report of each table to learn the report-only codes and
write its observations. Deeper geography codes are added when a report that
contains them is generated (:func:`app.connectors.base.upsert_discovered_codes`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.config import settings as global_settings
from app.connectors.base import (
    EMPTY,
    FORMAT_CHANGED,
    NOT_FOUND,
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

CHANNEL = "secimdagitimapp"
PAGE = "secim.zul"
INSTITUTION = "tuik"
INSTITUTION_NAME = "Türkiye İstatistik Kurumu"
SOURCE_CATEGORY = "Seçim İstatistikleri"

TOTAL_CODE = "_T"
TOTAL_LABEL = "Toplam"

TABLE_LIST_HEADER = "Tablo seçimi"
YEAR_LIST_HEADER = "Yıllar"
PARTI_LIST_HEADER = "Siyasi parti"

# --- dimensions -----------------------------------------------------------

DIM_CEVRE = "SECIM_CEVRESI"
DIM_ILCE = "ILCE"
DIM_YERLESIM = "YERLESIM_YERI"
DIM_BOLGE = "SECIM_BOLGESI"
DIM_IL = "IL"
DIM_GUMRUK = "GUMRUK_KAPISI"
DIM_ULKE = "ULKE"
DIM_TEMSILCILIK = "TEMSILCILIK"
DIM_REGION = "BOLGE"
DIM_PARTI = "PARTI"
DIM_MEASURE = "MEASURE"
DIM_ADAY = "ADAY"

GEO_DIMENSIONS: dict[str, tuple[str, str]] = {
    DIM_CEVRE: ("Seçim çevresi", ROLE_GEO),
    DIM_ILCE: ("İlçe", ROLE_GEO),
    DIM_YERLESIM: ("Yerleşim yeri", ROLE_GEO),
    DIM_BOLGE: ("Seçim bölgesi", ROLE_GEO),
    DIM_IL: ("İl", ROLE_GEO),
    DIM_GUMRUK: ("Gümrük kapısı", ROLE_GEO),
    DIM_ULKE: ("Ülke", ROLE_GEO),
    DIM_TEMSILCILIK: ("Temsilcilik", ROLE_GEO),
    DIM_REGION: ("Bölge", ROLE_GEO),
}
DIM_LABELS: dict[str, str] = {
    DIM_PARTI: "Siyasi parti",
    DIM_MEASURE: "Ölçü",
    DIM_ADAY: "Aday",
    "TIME_PERIOD": "Seçim dönemi",
    **{code: label for code, (label, _role) in GEO_DIMENSIONS.items()},
}

# --- measures -------------------------------------------------------------

M_VOTES = "VOTES"
M_VOTE_SHARE = "VOTE_SHARE"
M_SANDIK = "SANDIK_SAYISI"
M_KAYITLI = "KAYITLI_SECMEN"
M_OY_KULLANAN = "OY_KULLANAN"
M_GECERLI = "GECERLI_OY"
M_KATILIM = "KATILIM_ORANI"
M_MILLETVEKILI = "MILETVEKILI_SAYISI"
M_ELECTED = "ELECTED"

# Order used for the MEASURE code list of every dataset. Only the codes a table
# can emit actually appear in series; the full list is harmless for validation.
MEASURE_CODES: tuple[tuple[str, str], ...] = (
    (M_VOTES, "Alınan oy sayısı"),
    (M_VOTE_SHARE, "Oy oranı (%)"),
    (M_SANDIK, "Sandık sayısı"),
    (M_KAYITLI, "Kayıtlı seçmen sayısı"),
    (M_OY_KULLANAN, "Oy kullanan seçmen sayısı"),
    (M_GECERLI, "Geçerli oy sayısı"),
    (M_KATILIM, "Katılım oranı (%)"),
    (M_MILLETVEKILI, "Milletvekili sayısı"),
    (M_ELECTED, "Kazandı"),
)
_MEASURE_BY_TOKEN: tuple[tuple[str, str], ...] = (
    ("Sandık", M_SANDIK),
    ("Kayıtlı seçmen", M_KAYITLI),
    ("Seçmen Sayısı", M_KAYITLI),
    ("Oy kullanan", M_OY_KULLANAN),
    ("Katılım", M_KATILIM),
    ("Geçerli oy", M_GECERLI),
    ("Milletvekili", M_MILLETVEKILI),
)

# Report labels that mean "all members of this dimension".
TOTAL_LABELS: frozenset[str] = frozenset(
    {
        "Seçim çevresi toplamı",
        "Gümrük kapıları toplamı",
        "Türkiye",
        "Türkiye ülke geneli",
        "Seçim çevresi",
        "Genel Toplam",
        "Toplam",
        "Yurt dışı toplam",
        "Yurt dışı toplamı",
    }
)

# --- elections ------------------------------------------------------------

# Form labels and report title fragments mapped to the real election date.
ELECTION_DATES: dict[str, tuple[int, int, int]] = {
    "2023": (2023, 5, 14),
    "2018": (2018, 6, 24),
    "2015 (1 Kasım)": (2015, 11, 1),
    "2015 (7 Haziran)": (2015, 6, 7),
    "2015 seçimi (1 Kasım)": (2015, 11, 1),
    "2015 seçimi (7 Kasım)": (2015, 11, 1),
    "2015 seçimi (7 Haziran)": (2015, 6, 7),
    "1 Kasım 2015": (2015, 11, 1),
    "7 Haziran 2015": (2015, 6, 7),
    "2011": (2011, 6, 12),
    "2011 seçimi": (2011, 6, 12),
    "2007": (2007, 7, 22),
    "2007 seçimi": (2007, 7, 22),
    "2002": (2002, 11, 3),
    "2002 seçimi": (2002, 11, 3),
    "1999": (1999, 4, 18),
    "1999 seçimi": (1999, 4, 18),
    "1995": (1995, 12, 24),
    "1995 seçimi": (1995, 12, 24),
    "1991": (1991, 10, 20),
    "1991 seçimi": (1991, 10, 20),
    "1987": (1987, 11, 29),
    "1983": (1983, 11, 6),
    "1977": (1977, 6, 5),
    "1973": (1973, 10, 14),
    "1969": (1969, 10, 12),
    "1965": (1965, 10, 10),
    "1961": (1961, 10, 15),
    "1957": (1957, 10, 27),
    "1954": (1954, 5, 2),
    "1950": (1950, 5, 14),
}

TURKISH_MONTHS: dict[str, int] = {
    "Ocak": 1,
    "Şubat": 2,
    "Mart": 3,
    "Nisan": 4,
    "Mayıs": 5,
    "Haziran": 6,
    "Temmuz": 7,
    "Ağustos": 8,
    "Eylül": 9,
    "Ekim": 10,
    "Kasım": 11,
    "Aralık": 12,
}
_DAY_MONTH = re.compile(r"(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)")
_YEAR = re.compile(r"(19|20)\d{2}")

YEAR_RADIOS: tuple[str, ...] = (
    "2023",
    "2018",
    "2015 seçimi (1 Kasım)",
    "2015 seçimi (7 Haziran)",
    "2011 seçimi",
    "2007 seçimi",
    "2002 seçimi",
    "1999 seçimi",
    "1995 seçimi",
    "1991 seçimi",
)
RANGE_RADIOS: tuple[str, ...] = ("2023-1983 seçimi", "1977-1950 seçimi")

LIST_DIMENSIONS: dict[str, str] = {
    "Seçim çevresi": DIM_CEVRE,
    "İlçe": DIM_ILCE,
    "Yerleşim yeri": DIM_YERLESIM,
    "Seçim bölgesi": DIM_BOLGE,
    "İl (Gümrük kapısı)": DIM_IL,
    "Ülkeler": DIM_ULKE,
    "Siyasi parti": DIM_PARTI,
}


def _elections_by_year() -> dict[int, set[date]]:
    """Every known election date, grouped by year (from :data:`ELECTION_DATES`)."""
    grouped: dict[int, set[date]] = {}
    for year, month, day in ELECTION_DATES.values():
        grouped.setdefault(year, set()).add(date(year, month, day))
    return grouped


_ELECTIONS_BY_YEAR: dict[int, set[date]] = _elections_by_year()


def election_date(value: str) -> date | None:
    """Resolve a form label or report title fragment to the real election date.

    Only exact dates are returned: an explicit ``YYYY-MM-DD``, a known label, or
    a day/month/year phrase. A bare year is accepted only when that year has a
    single known election; an ambiguous year (e.g. ``2015``, which had two
    elections) resolves to ``None`` so callers fail instead of inventing a date.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    if text in ELECTION_DATES:
        return date(*ELECTION_DATES[text])
    day_month = next(
        (match for match in _DAY_MONTH.finditer(text) if match.group(2) in TURKISH_MONTHS),
        None,
    )
    year = _YEAR.search(text)
    if day_month and year:
        return date(int(year.group(0)), TURKISH_MONTHS[day_month.group(2)], int(day_month.group(1)))
    if year:
        known = _ELECTIONS_BY_YEAR.get(int(year.group(0)), set())
        if len(known) == 1:
            return next(iter(known))
    return None


def election_year(value: str) -> int | None:
    match = _YEAR.search(value or "")
    return int(match.group(0)) if match else None


def _election_target(value: str) -> date | None:
    """Resolve a form label or an ISO date to the election date."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return election_date(value)


# --- table definitions ----------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    """One election table: how it is driven and how its report is read."""

    index: int
    code: str
    table_label: str
    name: str
    description: str
    walk: str  # "radio" | "year_list" | "range"
    interpreter: str  # "generic" | "by_year" | "candidates" | "seats" | "national"
    dimensions: tuple[str, ...]  # non-time dimensions in order
    label_dims: tuple[str | None, ...] = ()
    mode: bool = True
    levels: tuple[str, ...] = ()
    aggregate_headers: tuple[str, ...] = ()
    frequency: str = "irregular"


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        index=0,
        code="TUIK_SECIM_YIL",
        table_label="Seçim yılına göre milletvekili genel seçimi sonuçları",
        name="TÜİK Seçim — Seçim yılına göre milletvekili genel seçimi sonuçları",
        description=(
            "Milletvekili genel seçimi sonuçları seçim yılına göre; alınan oy "
            "sayısı, oy oranı ve çıkarılan milletvekili sayısı (secimdagitimapp)."
        ),
        walk="range",
        interpreter="by_year",
        dimensions=(DIM_CEVRE, DIM_PARTI, DIM_MEASURE),
        mode=False,
        levels=("Seçim çevresi", PARTI_LIST_HEADER),
        aggregate_headers=(PARTI_LIST_HEADER,),
    ),
    TableSpec(
        index=1,
        code="TUIK_SECIM_CEVRE_ILCE",
        table_label="Seçim çevresi ve ilçelere göre milletvekili genel seçimi sonuçları",
        name="TÜİK Seçim — Seçim çevresi ve ilçelere göre genel seçim sonuçları",
        description=(
            "Seçim çevresi ve ilçelere göre milletvekili genel seçimi sonuçları "
            "(sandık, seçmen, geçerli oy ve parti oyları)(secimdagitimapp)."
        ),
        walk="year_list",
        interpreter="generic",
        dimensions=(DIM_CEVRE, DIM_ILCE, DIM_YERLESIM, DIM_PARTI, DIM_MEASURE),
        label_dims=(None, DIM_CEVRE, DIM_ILCE, DIM_YERLESIM),
        levels=(YEAR_LIST_HEADER, "Seçim çevresi"),
    ),
    TableSpec(
        index=2,
        code="TUIK_SECIM_CEVRE_BOLGE",
        table_label="Seçim çevresi ve bölgelerine göre milletvekili genel seçimi sonuçları",
        name="TÜİK Seçim — Seçim çevresi ve bölgelerine göre genel seçim sonuçları",
        description=(
            "Seçim çevresi ve bölgelerine göre milletvekili genel seçimi "
            "sonuçları; mahalle/köy düzeyine iner (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="generic",
        dimensions=(DIM_CEVRE, DIM_ILCE, DIM_YERLESIM, DIM_PARTI, DIM_MEASURE),
        label_dims=(None, DIM_CEVRE, DIM_ILCE, DIM_YERLESIM, None),
        levels=("Seçim çevresi", "İlçe"),
    ),
    TableSpec(
        index=3,
        code="TUIK_SECIM_CEVRE_SANDIK",
        table_label="Seçim çevresi ve sandık kurullarına göre milletvekili genel seçimi sonuçları",
        name="TÜİK Seçim — Seçim çevresi ve sandık kurullarına göre genel seçim sonuçları",
        description=(
            "Seçim çevresi ve sandık kurullarına göre milletvekili genel seçimi "
            "sonuçları; yerleşim yeri ve seçim bölgesine iner (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="generic",
        dimensions=(DIM_CEVRE, DIM_ILCE, DIM_BOLGE, DIM_PARTI, DIM_MEASURE),
        label_dims=(None, DIM_CEVRE, DIM_ILCE, DIM_BOLGE, None),
        levels=("Seçim çevresi", "İlçe", "Yerleşim yeri", "Seçim bölgesi"),
    ),
    TableSpec(
        index=4,
        code="TUIK_SECIM_ADAY",
        table_label="Milletvekili genel seçimine katılan adaylar",
        name="TÜİK Seçim — Milletvekili genel seçimine katılan adaylar",
        description=(
            "Seçim çevresi ve siyasi partiye göre milletvekili genel seçimine "
            "katılan adaylar ve seçilme durumu (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="candidates",
        dimensions=(DIM_CEVRE, DIM_PARTI, DIM_ADAY, DIM_MEASURE),
        mode=False,
        levels=("Seçim çevresi", PARTI_LIST_HEADER),
        aggregate_headers=(PARTI_LIST_HEADER,),
    ),
    TableSpec(
        index=5,
        code="TUIK_SECIM_MILLETVEKILI_SAYISI",
        table_label=(
            "Yıllara ve illere göre siyasi parti ve bağımsızların çıkardığı milletvekili sayısı"
        ),
        name="TÜİK Seçim — Siyasi parti ve bağımsızların çıkardığı milletvekili sayısı",
        description=(
            "Yıllara ve illere göre siyasi parti ve bağımsızların çıkardığı "
            "milletvekili sayısı (secimdagitimapp)."
        ),
        walk="year_list",
        interpreter="seats",
        dimensions=(DIM_CEVRE, DIM_PARTI, DIM_MEASURE),
        mode=False,
        levels=(YEAR_LIST_HEADER, "Seçim çevresi"),
    ),
    TableSpec(
        index=6,
        code="TUIK_SECIM_GUMRUK",
        table_label="Gümrük kapıları seçim sonucu",
        name="TÜİK Seçim — Gümrük kapıları seçim sonucu",
        description=(
            "Gümrük kapıları seçim sonucu; il, ilçe ve gümrük kapısı kırılımıyla (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="generic",
        dimensions=(DIM_IL, DIM_ILCE, DIM_GUMRUK, DIM_PARTI, DIM_MEASURE),
        label_dims=(DIM_IL, DIM_ILCE, DIM_GUMRUK),
        levels=("İl (Gümrük kapısı)",),
    ),
    TableSpec(
        index=7,
        code="TUIK_SECIM_ULKE_GENELI",
        table_label="Milletvekili genel seçimi sonucu",
        name="TÜİK Seçim — Milletvekili genel seçimi sonucu (ülke geneli)",
        description=(
            "Milletvekili genel seçimi ülke geneli sonucu; yurt içi, yurt dışı, "
            "gümrük kapıları ve ittifak kırılımlarıyla (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="national",
        dimensions=(DIM_REGION, DIM_PARTI, DIM_MEASURE),
        mode=False,
        levels=(),
    ),
    TableSpec(
        index=8,
        code="TUIK_SECIM_CEVRE",
        table_label="Seçim çevresine göre milletvekili seçimi sonuçları",
        name="TÜİK Seçim — Seçim çevresine göre milletvekili seçimi sonuçları",
        description=(
            "Seçim çevresine (il) göre milletvekili seçimi sonuçları; ülke "
            "geneli, yurt içi ve yurt dışı kırılımlarıyla (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="generic",
        dimensions=(DIM_CEVRE, DIM_REGION, DIM_PARTI, DIM_MEASURE),
        label_dims=(DIM_CEVRE, DIM_REGION),
        levels=("Seçim çevresi",),
    ),
    TableSpec(
        index=9,
        code="TUIK_SECIM_YURTDISI",
        table_label="Yurt dışı sandıkları seçim sonucu",
        name="TÜİK Seçim — Yurt dışı sandıkları seçim sonucu",
        description=(
            "Yurt dışı sandıkları seçim sonucu; ülke ve temsilcilik kırılımıyla (secimdagitimapp)."
        ),
        walk="radio",
        interpreter="generic",
        dimensions=(DIM_ULKE, DIM_TEMSILCILIK, DIM_PARTI, DIM_MEASURE),
        label_dims=(DIM_ULKE, DIM_TEMSILCILIK),
        levels=("Ülkeler",),
    ),
)
TABLE_SPEC_BY_CODE = {spec.code: spec for spec in TABLE_SPECS}
TABLE_SPEC_BY_INDEX = {spec.index: spec for spec in TABLE_SPECS}


# --- result types ---------------------------------------------------------


@dataclass
class InterpretedSeries:
    """One interpreted election series (codes plus observation points)."""

    codes: dict[str, str]
    points: list[tuple[date, Decimal | None]]
    title: str = ""
    dataset: str = ""
    raw_object_keys: list[str] = field(default_factory=list)


@dataclass
class WalkResult:
    """Code lists and (optionally) one report collected from a form path."""

    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    years: list[str] = field(default_factory=list)
    parties: list[str] = field(default_factory=list)
    grid: ReportGrid | None = None
    raw_keys: list[str] = field(default_factory=list)
    incomplete: bool = False

    def add(self, dimension: str, value: str, label: str | None = None) -> None:
        if not value:
            return
        self.codes.setdefault(dimension, {})[value] = value
        self.labels.setdefault(dimension, {})[value] = label or value

    def add_total(self, dimension: str) -> None:
        self.codes.setdefault(dimension, {})[TOTAL_CODE] = TOTAL_CODE
        self.labels.setdefault(dimension, {})[TOTAL_CODE] = TOTAL_LABEL


@dataclass
class ReportPlan:
    """One report to generate for discovery or a headline."""

    dataset_code: str
    spec: TableSpec
    election: str
    mode: str = M_VOTES

    @property
    def page(self) -> str:
        return PAGE

    @property
    def period(self) -> date | None:
        return election_date(self.election)


# --- form driving ---------------------------------------------------------


def _table_box(client: LegacyZkClient) -> str | None:
    for box_id in client.listboxes:
        if client.list_header(box_id) == TABLE_LIST_HEADER:
            return box_id
    return None


def select_table(client: LegacyZkClient, spec: TableSpec) -> None:
    """Select the table's item in the bootstrap-filled "Tablo seçimi" list."""
    box_id = _table_box(client)
    if box_id is None:
        raise ConnectorError(FORMAT_CHANGED, f"{PAGE}: no {TABLE_LIST_HEADER!r} listbox")
    items = client.list_items(box_id)
    match = next((item for item in items if item.label == spec.table_label), None)
    if match is None:
        raise ConnectorError(FORMAT_CHANGED, f"{PAGE}: table {spec.table_label!r} not listed")
    client.select_items(box_id, [match.id])


def _visible_radios(client: LegacyZkClient, labels: Sequence[str]) -> list[str]:
    """The subset of ``labels`` whose radio is present and currently visible."""
    return [
        label
        for label in labels
        if label in client.radios and client._visible.get(client.radios[label].id, False)
    ]


def _visible_year_radios(client: LegacyZkClient) -> list[str]:
    return _visible_radios(client, YEAR_RADIOS)


def _range_radios(client: LegacyZkClient) -> list[str]:
    """The range radios; both exist regardless, so fall back to presence."""
    return _visible_radios(client, RANGE_RADIOS) or [
        label for label in RANGE_RADIOS if label in client.radios
    ]


def _resolve_election(election: str | None, options: Sequence[str]) -> str | None:
    """Match a requested election (form label or ISO date) to one offered option."""
    if election is None:
        return None
    if election in options:
        return election
    target = _election_target(election)
    if target is None:
        return None
    return next((option for option in options if election_date(option) == target), None)


def _checkbox_value(client: LegacyZkClient, label: str) -> bool:
    component = client.radios.get(label)
    return bool(component and client._visible.get(component.id))


def _check_election(client: LegacyZkClient, spec: TableSpec, election: str | None) -> str | None:
    """Check the election radio/range for radio-driven tables."""
    if spec.walk == "range":
        options = _range_radios(client)
        label = _resolve_election(election, options)
        if label is None and election is None:
            label = options[0] if options else None
        if label is None and election is not None:
            raise ConnectorError(
                NOT_FOUND, f"{PAGE}: election {election!r} not offered by {spec.code}"
            )
        if label is None:
            raise ConnectorError(FORMAT_CHANGED, f"{PAGE}: no range radio")
        client.check_radio(label)
        return label

    options = _visible_year_radios(client)
    label = _resolve_election(election, options)
    if label is None and election is None:
        label = options[0] if options else None
    if label is None and election is not None:
        raise ConnectorError(NOT_FOUND, f"{PAGE}: election {election!r} not offered by {spec.code}")
    if label is None:
        raise ConnectorError(FORMAT_CHANGED, f"{PAGE}: no election radio for {spec.code}")
    client.check_radio(label)
    return label


def _select_year(client: LegacyZkClient, election: str | None) -> str | None:
    """Select one year in a "Yıllar" list (year-list tables)."""
    box_id = next(
        (b for b in client.visible_listboxes() if client.list_header(b) == YEAR_LIST_HEADER), None
    )
    if box_id is None:
        return None
    items = client.list_items(box_id)
    match = next((item for item in items if item.label == election), None)
    if match is None and election is not None:
        target = _election_target(election)
        if target is not None:
            match = next((item for item in items if _election_target(item.label) == target), None)
    if match is None and election is not None:
        raise ConnectorError(
            NOT_FOUND, f"{PAGE}: election {election!r} not offered by the {YEAR_LIST_HEADER!r} list"
        )
    if match is None:
        match = items[0] if items else None
    if match is None:
        return None
    client.select_items(box_id, [match.id])
    return match.label


def _register_elections(result: WalkResult, labels: Sequence[str]) -> None:
    for label in labels:
        if label and label not in result.years:
            result.years.append(label)


# Server validation text shown when the requested election is not selectable.
_UNAVAILABLE_ELECTION = ("Seçim yılı seçiniz", "Yıl seçiniz")


def _election_unavailable(message: str) -> bool:
    return any(token in message for token in _UNAVAILABLE_ELECTION)


def _register_items(result: WalkResult, header: str, items: Sequence[ListItem]) -> None:
    if header == YEAR_LIST_HEADER:
        for item in items:
            if item.label not in result.years:
                result.years.append(item.label)
        return
    dimension = LIST_DIMENSIONS.get(header)
    if dimension is None:
        return
    has_total = False
    for item in items:
        if item.label.startswith("<<"):
            has_total = True
            continue
        result.add(dimension, item.label)
        if dimension == DIM_PARTI and item.label not in result.parties:
            result.parties.append(item.label)
    if has_total:
        result.add_total(dimension)


def _pick_item(items: Sequence[ListItem], *, aggregate: bool) -> ListItem | None:
    if aggregate:
        for item in items:
            if item.label.startswith("<<"):
                return item
    for item in items:
        if not item.label.startswith("<<"):
            return item
    return items[0] if items else None


def walk_form(
    client: LegacyZkClient,
    spec: TableSpec,
    *,
    election: str | None = None,
    mode: str = M_VOTES,
    generate: bool = False,
    budget: int = 250,
) -> WalkResult:
    """Drive one table's form, collecting list codes and optionally a report.

    Registration is exhaustive for every list the form exposes; to reveal the
    next geography level the walk selects the ``<< … >>`` aggregate when the
    form offers one, else the first item (matching the "smallest report" rule).
    A non-zero budget bounds the live load; when it is exhausted ``incomplete``
    is set and the codes learned from reports fill the rest.
    """
    client.bootstrap()
    result = WalkResult()
    select_table(client, spec)
    if spec.walk == "radio":
        _check_election(client, spec, election)
        _register_elections(result, _visible_year_radios(client))
    elif spec.walk == "range":
        _check_election(client, spec, election)
        _register_elections(result, _range_radios(client))
    if spec.mode:
        client.check_radio("Oransal sonuç" if mode == M_VOTE_SHARE else "Mutlak sonuç")
    if spec.walk == "year_list":
        _select_year(client, election)

    handled: set[str] = set()
    requests = 0
    for _ in range(len(spec.levels) + 4):
        progressed = False
        for box_id in client.visible_listboxes():
            header = client.list_header(box_id)
            if header == TABLE_LIST_HEADER or box_id in handled:
                continue
            items = client.list_items(box_id)
            if not items:
                continue
            _register_items(result, header, items)
            handled.add(box_id)
            progressed = True
            if header == YEAR_LIST_HEADER:
                # ``_select_year`` already chose the report's election.
                continue
            if header in spec.levels:
                pick = _pick_item(items, aggregate=header in spec.aggregate_headers)
                if pick is not None:
                    client.select_items(box_id, [pick.id])
                    requests += 1
            if requests >= budget:
                result.incomplete = True
                break
        if not progressed or result.incomplete:
            break
    if generate:
        try:
            content, _url = client.generate_report()
        except ConnectorError as exc:
            if _election_unavailable(exc.message):
                raise ConnectorError(NOT_FOUND, exc.message) from exc
            raise
        result.grid = parse_report_grid(content)
    result.raw_keys = client.take_raw_keys()
    return result


# --- report interpretation helpers ---------------------------------------


def _is_measure_header(text: str) -> str | None:
    for token, code in _MEASURE_BY_TOKEN:
        if token in text:
            return code
    return None


def find_reference_header(grid: ReportGrid) -> int | None:
    """Locate the column-header row (the one naming the value columns)."""
    best_index = None
    best_score = 0
    for index, row in enumerate(grid.rows[:6]):
        score = sum(1 for cell in row if _is_measure_header(cell))
        if score > best_score:
            best_score = score
            best_index = index
    if best_index is None or best_score < 2:
        return None
    return best_index


def _label_code(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text in TOTAL_LABELS:
        return TOTAL_CODE
    return text


def _label_codes(
    spec: TableSpec, row: Sequence[str], last: dict[int, str | None]
) -> dict[str, str]:
    """Resolve a row's positional label cells into dimension codes with carry."""
    codes: dict[str, str] = {}
    for column, dimension in enumerate(spec.label_dims):
        if dimension is None:
            continue
        raw = row[column] if column < len(row) else ""
        code = _label_code(raw)
        if code is not None:
            codes[dimension] = code
            last[column] = code
            for lower in [key for key in last if key > column]:
                del last[lower]
        else:
            codes[dimension] = last.get(column) or TOTAL_CODE
    return codes


def _party_measure(header: str, mode: str) -> tuple[str, str]:
    """Return ``(PARTI, MEASURE)`` for one value column header."""
    fixed = _is_measure_header(header)
    if fixed is not None:
        return TOTAL_CODE, fixed
    return header, M_VOTE_SHARE if mode == M_VOTE_SHARE else M_VOTES


def _report_period(spec: TableSpec, period: date | None) -> date:
    """The selected election's exact date, or a clear failure (never a guess)."""
    if period is None:
        raise ConnectorError(FORMAT_CHANGED, f"{spec.code}: report has no resolvable election date")
    return period


def interpret_generic(
    grid: ReportGrid,
    spec: TableSpec,
    *,
    period: date | None,
    mode: str = M_VOTES,
    parties: Sequence[str] = (),
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret a "label columns + value columns" report."""
    point_period = _report_period(spec, period)
    header_index = find_reference_header(grid)
    if header_index is None:
        raise ConnectorError(FORMAT_CHANGED, f"{spec.code}: report has no column header row")
    header = grid.rows[header_index]
    label_count = len(spec.label_dims)
    value_columns = [column for column in range(label_count, len(header)) if header[column].strip()]
    if not value_columns:
        raise ConnectorError(FORMAT_CHANGED, f"{spec.code}: report has no value columns")
    series: dict[tuple[str, ...], InterpretedSeries] = {}
    uninterpreted: list[str] = []
    last: dict[int, str | None] = {}
    for row in grid.rows[header_index + 1 :]:
        values = {
            column: parse_turkish_number(row[column]) if column < len(row) else None
            for column in value_columns
        }
        if all(value is None for value in values.values()):
            continue
        labels = _label_codes(spec, row, last)
        if any(code is None for code in labels.values()):
            uninterpreted.append(row[0] if row else "")
            continue
        for column in value_columns:
            value = values[column]
            if value is None:
                continue
            party, measure = _party_measure(header[column], mode)
            codes = {**labels, DIM_PARTI: party, DIM_MEASURE: measure}
            key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
            entry = series.get(key)
            point = (point_period, value)
            if entry is None:
                series[key] = InterpretedSeries(codes=codes, points=[point], title=grid.title)
            else:
                entry.points.append(point)
    for entry in series.values():
        entry.points = sorted(entry.points)
    return list(series.values()), uninterpreted


def interpret_by_year(
    grid: ReportGrid, spec: TableSpec, *, mode: str = M_VOTES
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret table 0: election years across columns, metric/party down rows."""
    year_row = None
    for index, row in enumerate(grid.rows[:6]):
        found = {column: election_date(cell) for column, cell in enumerate(row) if cell}
        found = {column: value for column, value in found.items() if value is not None}
        if len(found) >= 3:
            year_row = found
            header_index = index
            break
    if year_row is None:
        raise ConnectorError(FORMAT_CHANGED, "TUIK_SECIM_YIL: report has no election-year row")
    columns = {column: value for column, value in year_row.items() if value is not None}
    series: dict[tuple[str, ...], InterpretedSeries] = {}
    uninterpreted: list[str] = []
    for row in grid.rows[header_index + 1 :]:
        if not row:
            continue
        name = row[0].strip() if row[0] else ""
        letter = row[1].strip() if len(row) > 1 and row[1] else ""
        if letter in ("A", "B", "C") and name:
            measure = {"A": M_VOTES, "B": M_VOTE_SHARE, "C": M_MILLETVEKILI}[letter]
            party = name
        elif name and not letter:
            measure = _is_measure_header(name)
            if measure is None:
                continue
            party = TOTAL_CODE
        else:
            continue
        for column, election in columns.items():
            value = parse_turkish_number(row[column]) if column < len(row) else None
            if value is None:
                continue
            codes = {DIM_CEVRE: TOTAL_CODE, DIM_PARTI: party, DIM_MEASURE: measure}
            key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
            point = (election, value)
            entry = series.get(key)
            if entry is None:
                series[key] = InterpretedSeries(codes=codes, points=[point], title=grid.title)
            else:
                entry.points.append(point)
    for entry in series.values():
        entry.points = sorted(entry.points)
    return list(series.values()), uninterpreted


def interpret_seats(
    grid: ReportGrid, spec: TableSpec, *, period: date | None = None
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret table 5: year / province / party / seat count rows.

    Older reports carry ``year, province, party, value``; from 2015 the election
    month is a separate column (``year, (1 Kasım), province, party, value``). The
    party and value columns are located from the header so both shapes read
    alike, and the selected election supplies the exact date.
    """
    election = _report_period(spec, period)
    header_index = None
    header: Sequence[str] = ()
    for index, row in enumerate(grid.rows[:6]):
        if any(_is_measure_header(cell) for cell in row):
            header_index = index
            header = row
            break
    if header_index is None:
        raise ConnectorError(FORMAT_CHANGED, "TUIK_SECIM_MILLETVEKILI_SAYISI: no header row")
    value_col = next(c for c, cell in enumerate(header) if _is_measure_header(cell))
    party_col = value_col - 1
    province_col = party_col - 1
    series: dict[tuple[str, ...], InterpretedSeries] = {}
    uninterpreted: list[str] = []
    current_year: str | None = None
    current_province: str | None = None
    for row in grid.rows[header_index + 1 :]:
        if len(row) <= value_col:
            continue
        year = row[0].strip()
        province = row[province_col].strip() if 0 <= province_col < len(row) else ""
        party = row[party_col].strip() if 0 <= party_col < len(row) else ""
        value = parse_turkish_number(row[value_col])
        if year:
            current_year = year
        if province:
            current_province = province
        if value is None:
            continue
        # The row label carries the row's own election when it resolves; when it
        # cannot (e.g. a bare, ambiguous ``2015``) the selected election stands.
        row_parts = [current_year or ""] + [
            row[c].strip() for c in range(1, province_col) if c < len(row) and row[c].strip()
        ]
        row_label = " ".join(part for part in row_parts if part)
        row_date = election_date(row_label)
        if row_date is not None and row_date != election:
            logger.warning(
                "%s: report row %r says %s but the selected election is %s",
                spec.code,
                row_label,
                row_date,
                election,
            )
        point_date = row_date if row_date is not None else election
        if not party:
            uninterpreted.append(year or province)
            continue
        codes = {
            DIM_CEVRE: current_province or TOTAL_CODE,
            DIM_PARTI: party,
            DIM_MEASURE: M_MILLETVEKILI,
        }
        key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
        point = (point_date, value)
        entry = series.get(key)
        if entry is None:
            series[key] = InterpretedSeries(codes=codes, points=[point], title=grid.title)
        else:
            entry.points.append(point)
    for entry in series.values():
        entry.points = sorted(entry.points)
    return list(series.values()), uninterpreted


def _is_party_header(name: str, party_set: set[str]) -> bool:
    """Whether a candidate-table row names a party rather than a person."""
    if name.casefold() in party_set:
        return True
    if not party_set:
        return False
    # Older reports (e.g. 1991) label parties by abbreviation ("ANAP") that the
    # form's full-name party list never matches; those headers are the only
    # all-uppercase names in the table (candidates are personal names).
    return name.isupper() and any(character.isalpha() for character in name)


def interpret_candidates(
    grid: ReportGrid,
    spec: TableSpec,
    *,
    period: date | None = None,
    parties: Sequence[str] = (),
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret table 4: year / province, party, candidate, elected flag."""
    election = _report_period(spec, period)
    party_set = {party.casefold() for party in parties}
    series: dict[tuple[str, ...], InterpretedSeries] = {}
    uninterpreted: list[str] = []
    current_province: str | None = None
    current_party: str | None = None
    for row in grid.rows[2:]:
        if len(row) < 3:
            continue
        year = row[0].strip()
        flag = row[1].strip()
        name = row[2].strip()
        if not name or name == "(*) Kazandı":
            continue
        if year:
            current_province = name
            current_party = None
            row_date = election_date(year)
            if row_date is not None and row_date != election:
                logger.warning(
                    "%s: report row %r says %s but the selected election is %s",
                    spec.code,
                    year,
                    row_date,
                    election,
                )
            continue
        if flag:
            if current_party is None or current_province is None:
                continue
            codes = {
                DIM_CEVRE: current_province,
                DIM_PARTI: current_party,
                DIM_ADAY: name,
                DIM_MEASURE: M_ELECTED,
            }
            key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
            series[key] = InterpretedSeries(
                codes=codes,
                points=[(election, Decimal(1) if flag == "*" else Decimal(0))],
                title=grid.title,
            )
            continue
        # No year and no elected flag: a party header or, once a party is
        # active, a candidate who was not elected.
        if _is_party_header(name, party_set):
            current_party = name
            continue
        if current_party is None or current_province is None:
            continue
        codes = {
            DIM_CEVRE: current_province,
            DIM_PARTI: current_party,
            DIM_ADAY: name,
            DIM_MEASURE: M_ELECTED,
        }
        key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
        series[key] = InterpretedSeries(
            codes=codes,
            points=[(election, Decimal(0))],
            title=grid.title,
        )
    return list(series.values()), uninterpreted


def interpret_national(
    grid: ReportGrid, spec: TableSpec, *, period: date | None
) -> tuple[list[InterpretedSeries], list[str]]:
    """Interpret table 7: national blocks with count rows and split percent rows."""
    point_period = _report_period(spec, period)
    header_index = find_reference_header(grid)
    if header_index is None:
        raise ConnectorError(FORMAT_CHANGED, "TUIK_SECIM_ULKE_GENELI: no header row")
    header = grid.rows[header_index]
    label_count = 1
    value_columns = [column for column in range(label_count, len(header)) if header[column].strip()]
    series: dict[tuple[str, ...], InterpretedSeries] = {}
    uninterpreted: list[str] = []
    current: str | None = None
    for row in grid.rows[header_index + 1 :]:
        label = row[0].strip() if row and row[0] else ""
        values = {
            column: parse_turkish_number(row[column]) if column < len(row) else None
            for column in value_columns
        }
        if label:
            current = label
            for column in value_columns:
                value = values[column]
                if value is None:
                    continue
                party, measure = _party_measure(header[column], M_VOTES)
                codes = {
                    DIM_REGION: _label_code(label) or label,
                    DIM_PARTI: party,
                    DIM_MEASURE: measure,
                }
                key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
                entry = series.get(key)
                point = (point_period, value)
                if entry is None:
                    series[key] = InterpretedSeries(codes=codes, points=[point], title=grid.title)
                else:
                    entry.points.append(point)
            continue
        if current is None:
            continue
        fixed_present = any(
            _is_measure_header(header[column]) is not None and values[column] is not None
            for column in value_columns
        )
        if not fixed_present:
            # The percent row carries only party columns (VOTE_SHARE).
            for column in value_columns:
                value = values[column]
                if value is None:
                    continue
                party, measure = _party_measure(header[column], M_VOTES)
                if measure != M_VOTES:
                    continue
                codes = {
                    DIM_REGION: _label_code(current) or current,
                    DIM_PARTI: party,
                    DIM_MEASURE: M_VOTE_SHARE,
                }
                key = tuple(codes.get(dimension, "") for dimension in spec.dimensions)
                entry = series.get(key)
                point = (point_period, value)
                if entry is None:
                    series[key] = InterpretedSeries(codes=codes, points=[point], title=grid.title)
                else:
                    entry.points.append(point)
    for entry in series.values():
        entry.points = sorted(entry.points)
    return list(series.values()), uninterpreted


def interpret_report(
    grid: ReportGrid,
    spec: TableSpec,
    *,
    period: date | None,
    mode: str = M_VOTES,
    parties: Sequence[str] = (),
) -> list[InterpretedSeries]:
    if spec.interpreter == "generic":
        series, _ = interpret_generic(grid, spec, period=period, mode=mode, parties=parties)
    elif spec.interpreter == "by_year":
        series, _ = interpret_by_year(grid, spec, mode=mode)
    elif spec.interpreter == "seats":
        series, _ = interpret_seats(grid, spec, period=period)
    elif spec.interpreter == "candidates":
        series, _ = interpret_candidates(grid, spec, period=period, parties=parties)
    elif spec.interpreter == "national":
        series, _ = interpret_national(grid, spec, period=period)
    else:  # pragma: no cover - guarded by the TableSpec literals
        raise ConnectorError(FORMAT_CHANGED, f"{spec.code}: unknown interpreter {spec.interpreter}")
    for entry in series:
        entry.dataset = spec.code
    return series


def distinct_series_count(series: Sequence[InterpretedSeries]) -> int:
    return len({(entry.dataset, tuple(sorted(entry.codes.items()))) for entry in series})


# --- connector ------------------------------------------------------------


class SecimConnector(SourceConnector):
    """Lists the election datasets and generates/loads their series."""

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

    def _default_client(self, dataset: str) -> LegacyZkClient:
        return LegacyZkClient(
            PAGE,
            base_url=self._settings.tuik_secim_base_url,
            settings_obj=self._settings,
            settings_prefix="tuik_secim",
            store=self._store,
            institution=INSTITUTION,
            dataset=dataset,
        )

    def _client(self, dataset: str) -> LegacyZkClient:
        return self._client_factory(dataset)

    def close(self) -> None:
        """No persistent connection; present so callers can close uniformly."""

    # -- catalog -----------------------------------------------------------

    def list_datasets(self, *, table: int | None = None) -> Iterator[DatasetMeta]:
        for spec in TABLE_SPECS:
            if table is not None and spec.index != table:
                continue
            client = self._client(spec.code)
            try:
                result = walk_form(client, spec, generate=False)
            finally:
                client.close()
            yield self._dataset_meta(spec, result)

    def _dataset_meta(self, spec: TableSpec, result: WalkResult) -> DatasetMeta:
        dimensions: list[DimensionMeta] = []
        position = 0
        periods = _table_periods(spec, result)
        dimensions.append(
            DimensionMeta(
                code="TIME_PERIOD",
                label=DIM_LABELS["TIME_PERIOD"],
                position=position,
                role=ROLE_TIME,
                codes=[DimensionCodeMeta(code=code, label=label) for code, label in periods],
                attributes={"frequency": spec.frequency, "elections": list(result.years)},
            )
        )
        position += 1
        for dimension in spec.dimensions:
            if dimension == DIM_PARTI:
                codes = self._codes(result, DIM_PARTI)
                dimensions.append(
                    DimensionMeta(
                        code=DIM_PARTI,
                        label=DIM_LABELS[DIM_PARTI],
                        position=position,
                        role=ROLE_OTHER,
                        codes=codes,
                        attributes={"source": "report"},
                    )
                )
            elif dimension == DIM_MEASURE:
                dimensions.append(
                    DimensionMeta(
                        code=DIM_MEASURE,
                        label=DIM_LABELS[DIM_MEASURE],
                        position=position,
                        role=ROLE_OTHER,
                        codes=[
                            DimensionCodeMeta(code=code, label=label)
                            for code, label in (
                                [(M_VOTES, "Alınan oy sayısı"), (M_VOTE_SHARE, "Oy oranı (%)")]
                                if spec.interpreter == "candidates"
                                else []
                            )
                            or list(MEASURE_CODES)
                        ],
                    )
                )
            elif dimension == DIM_ADAY:
                dimensions.append(
                    DimensionMeta(
                        code=DIM_ADAY,
                        label=DIM_LABELS[DIM_ADAY],
                        position=position,
                        role=ROLE_OTHER,
                        codes=[],
                        attributes={"source": "report"},
                    )
                )
            else:
                label, role = GEO_DIMENSIONS[dimension]
                dimensions.append(
                    DimensionMeta(
                        code=dimension,
                        label=label,
                        position=position,
                        role=role,
                        codes=self._codes(result, dimension),
                    )
                )
            position += 1
        coverage_start, coverage_end = _coverage()
        return DatasetMeta(
            external_code=spec.code,
            name=spec.name,
            description=spec.description,
            source_category=SOURCE_CATEGORY,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            attributes={
                "channel": CHANNEL,
                "page": PAGE,
                "table_index": spec.index,
                "table_label": spec.table_label,
                "default_frequency": spec.frequency,
                "interpreted": True,
                "walk": spec.walk,
                "modes": [M_VOTES, M_VOTE_SHARE] if spec.mode else [],
                "incomplete_catalog": result.incomplete,
                "elections": list(result.years),
            },
            dimensions=dimensions,
        )

    @staticmethod
    def _codes(result: WalkResult, dimension: str) -> list[DimensionCodeMeta]:
        known = dict(result.codes.get(dimension, {}))
        codes = [
            DimensionCodeMeta(code=code, label=result.labels.get(dimension, {}).get(code, code))
            for code in sorted(known)
            if code != TOTAL_CODE
        ]
        codes.insert(0, DimensionCodeMeta(code=TOTAL_CODE, label=TOTAL_LABEL, is_default=True))
        return codes

    def dataset_meta(
        self,
        dataset_code: str,
        *,
        table: int | None = None,
    ) -> DatasetMeta:
        spec = TABLE_SPEC_BY_CODE.get(dataset_code)
        if spec is None:
            raise ConnectorError(FORMAT_CHANGED, f"unknown dataset {dataset_code!r}")
        client = self._client(spec.code)
        try:
            result = walk_form(client, spec, generate=False)
        finally:
            client.close()
        return self._dataset_meta(spec, result)

    # -- discovery / reports ----------------------------------------------

    def discovery_plans(
        self,
        *,
        table: int | None = None,
        elections: int | None = None,
        catalog: dict[str, list[str]] | None = None,
    ) -> list[ReportPlan]:
        """One smallest report per table (newest ``elections`` of the table's own list).

        ``catalog`` holds each dataset's offered election labels as stored by the
        last catalog upsert; when a table is absent the form is walked afresh.
        """
        plans: list[ReportPlan] = []
        for spec in TABLE_SPECS:
            if table is not None and spec.index != table:
                continue
            offered = (catalog or {}).get(spec.code) or self._walk_elections(spec)
            for election in _discovery_elections(spec, elections, offered):
                plans.append(ReportPlan(spec.code, spec, election))
        return plans

    def _walk_elections(self, spec: TableSpec) -> list[str]:
        """The election labels one table's form really offers (a fresh walk)."""
        client = self._client(spec.code)
        try:
            result = walk_form(client, spec, generate=False)
        finally:
            client.close()
        return list(result.years)

    def build_report(self, plan: ReportPlan) -> list[InterpretedSeries]:
        """Generate and interpret one discovery/headline report."""
        period = plan.period
        if period is None:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{plan.dataset_code}: cannot resolve an exact election date for {plan.election!r}",
            )
        client = self._client(plan.dataset_code)
        try:
            result = walk_form(
                client,
                plan.spec,
                election=plan.election,
                mode=plan.mode,
                generate=True,
            )
        finally:
            client.close()
        if result.grid is None:
            return []
        title_date = election_date(result.grid.title)
        if title_date is not None and title_date != period:
            logger.warning(
                "%s: election %r resolved to %s but the report title %r says %s",
                plan.dataset_code,
                plan.election,
                period,
                result.grid.title,
                title_date,
            )
        series = interpret_report(
            result.grid,
            plan.spec,
            period=period,
            mode=plan.mode,
            parties=result.parties,
        )
        for entry in series:
            entry.raw_object_keys = result.raw_keys
        return series

    def register_discovered_codes(self, session: Any, dataset: Any, codes: dict[str, str]) -> None:
        upsert_discovered_codes(session, dataset, codes)

    # -- fetch -------------------------------------------------------------

    def fetch_series(
        self,
        dataset_code: str,
        codes: dict[str, str],
        *,
        order: list[str] | None = None,
        start: date = date(2000, 1, 1),
    ) -> FetchResult:
        spec = TABLE_SPEC_BY_CODE.get(dataset_code)
        if spec is None:
            raise ConnectorError(FORMAT_CHANGED, f"unknown dataset {dataset_code!r}")
        mode = M_VOTE_SHARE if codes.get(DIM_MEASURE) == M_VOTE_SHARE else M_VOTES
        election = _election_from_codes(codes)
        client = self._client(spec.code)
        try:
            result = walk_form(
                client,
                spec,
                election=election,
                mode=mode,
                generate=True,
            )
        finally:
            client.close()
        if result.grid is None:
            raise ConnectorError(EMPTY, f"{dataset_code}: no report generated")
        series = interpret_report(
            result.grid,
            spec,
            period=result_period(spec, election),
            mode=mode,
            parties=result.parties,
        )
        points: list[tuple[date, Decimal | None]] = []
        for entry in series:
            if all(entry.codes.get(dimension) == code for dimension, code in codes.items()):
                points.extend(entry.points)
        points = _merge_points(points)
        points = [(period, value) for period, value in points if period >= start]
        if not points:
            raise ConnectorError(
                EMPTY, f"{dataset_code}: no observations matched {codes} at or after {start}"
            )
        return FetchResult(
            external_code=_external_code(spec, codes),
            points=points,
            raw_object_keys=result.raw_keys,
            channel=CHANNEL,
        )


def result_period(spec: TableSpec, election: str | None) -> date | None:
    if election:
        return election_date(election)
    return None


# --- helpers --------------------------------------------------------------


def _period_codes() -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for year, month, day in ELECTION_DATES.values():
        value = date(year, month, day)
        seen[value.isoformat()] = f"{day} {_month_name(month)} {year}"
    return sorted(seen.items())


def _table_periods(spec: TableSpec, result: WalkResult) -> list[tuple[str, str]]:
    """The TIME_PERIOD codes one table really offers.

    The range table has no per-election radios, so it keeps every known election
    date (the union of the two range reports); every other table is limited to
    the elections its own form offered.
    """
    if spec.walk == "range":
        return _period_codes()
    return _election_period_codes(result.years)


def _election_period_codes(labels: Sequence[str]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for label in labels:
        value = election_date(label)
        if value is None:
            continue
        seen[value.isoformat()] = f"{value.day} {_month_name(value.month)} {value.year}"
    return sorted(seen.items())


def _month_name(month: int) -> str:
    for name, value in TURKISH_MONTHS.items():
        if value == month:
            return name
    return str(month)


def _coverage() -> tuple[date | None, date | None]:
    values = sorted({date(*parts) for parts in ELECTION_DATES.values()})
    if not values:
        return None, None
    return values[0], values[-1]


def _discovery_elections(spec: TableSpec, count: int | None, offered: Sequence[str]) -> list[str]:
    """The election label(s) to use for a table's smallest report."""
    if not offered:
        return []
    if spec.walk == "range":
        # Both ranges: the form splits the history into 1983–2023 and 1950–1977.
        return list(offered)
    ordered = sorted({option for option in offered}, key=_election_sort_key, reverse=True)
    return ordered[: max(1, count or 1)]


def _election_sort_key(label: str) -> tuple[int, int]:
    year = election_year(label) or 0
    date_value = election_date(label)
    return (year, date_value.month * 100 + date_value.day if date_value else 0)


def _election_from_codes(codes: dict[str, str]) -> str | None:
    return codes.get("TIME_PERIOD")


def _external_code(spec: TableSpec, codes: dict[str, str]) -> str:
    ordered = [codes[dimension] for dimension in spec.dimensions if dimension in codes]
    return f"{spec.code}:" + ".".join(ordered)


def _merge_points(
    points: Sequence[tuple[date, Decimal | None]],
) -> list[tuple[date, Decimal | None]]:
    by_date: dict[date, Decimal | None] = {}
    for period, value in points:
        if period not in by_date or by_date[period] is None:
            by_date[period] = value
    return sorted(by_date.items())


__all__ = [
    "CHANNEL",
    "DIM_ADAY",
    "DIM_BOLGE",
    "DIM_CEVRE",
    "DIM_GUMRUK",
    "DIM_IL",
    "DIM_ILCE",
    "DIM_MEASURE",
    "DIM_PARTI",
    "DIM_REGION",
    "DIM_TEMSILCILIK",
    "DIM_ULKE",
    "DIM_YERLESIM",
    "ELECTION_DATES",
    "GEO_DIMENSIONS",
    "INSTITUTION",
    "MEASURE_CODES",
    "PAGE",
    "TABLE_SPECS",
    "TABLE_SPEC_BY_CODE",
    "TABLE_SPEC_BY_INDEX",
    "TOTAL_CODE",
    "InterpretedSeries",
    "ReportPlan",
    "SecimConnector",
    "TableSpec",
    "WalkResult",
    "distinct_series_count",
    "election_date",
    "election_year",
    "find_reference_header",
    "interpret_candidates",
    "interpret_generic",
    "interpret_national",
    "interpret_by_year",
    "interpret_report",
    "interpret_seats",
    "parse_report_grid",
    "parse_turkish_number",
    "select_table",
    "walk_form",
]
