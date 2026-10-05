"""Deterministic catalog-enrichment rules (Task 2.2a; no Jev, no database).

Everything here is a pure function over plain, immutable views of a dataset
(:class:`DatasetView` and friends). The CLI (:mod:`app.catalog.enrich`) builds the
views from SQLAlchemy rows and writes the resulting plans; the tests build them
by hand. That split keeps every rule testable without a database.

Guiding principle: HIGH PRECISION. When a rule cannot decide, the field stays
``None`` (``pending``); the later Jev pass (2.2b) fills it. Never guess.

Text matching is case-insensitive with Turkish-aware folding (:func:`fold`):
``İ/ı/I/i`` all fold to ``i``, ``ş`` to ``s``, etc., so rule keywords written in
ASCII match either alphabet.

The module also owns the description template (:func:`dataset_description`) and
the period-series candidate grouping (:func:`period_series_candidates`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from app.catalog.tree import ConceptTree

# --------------------------------------------------------------------------- #
# Views (plain data; the CLI maps ORM rows onto these)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CodeView:
    """One dimension code with its label and source attributes."""

    code: str
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DimensionView:
    """One dataset dimension and its code list.

    ``is_measure``/``measure_source`` mirror the stored decision (rule, Jev or
    manual). When already decided, :func:`compute_dataset_plan` trusts them so a
    Jev-decided dimension is enumerated without re-running the classifier.
    """

    code: str
    label: str
    role: str
    codes: tuple[CodeView, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    position: int = 0
    is_measure: bool | None = None
    measure_source: str | None = None


@dataclass(frozen=True)
class DatasetView:
    """A dataset as the rules see it (no ORM, no session)."""

    institution_code: str
    institution_name: str
    external_code: str
    name: str
    description: str | None = None
    source_category: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    dimensions: tuple[DimensionView, ...] = ()


# --------------------------------------------------------------------------- #
# Turkish-aware folding
# --------------------------------------------------------------------------- #

_FOLD_MAP = str.maketrans(
    {
        "İ": "i",
        "I": "i",
        "ı": "i",
        "Ş": "s",
        "ş": "s",
        "Ğ": "g",
        "ğ": "g",
        "Ü": "u",
        "ü": "u",
        "Ö": "o",
        "ö": "o",
        "Ç": "c",
        "ç": "c",
    }
)


def fold(text: str | None) -> str:
    """Turkish-aware, case-insensitive fold used by every rule."""
    if not text:
        return ""
    return text.translate(_FOLD_MAP).casefold()


# --------------------------------------------------------------------------- #
# Measure dimensions
# --------------------------------------------------------------------------- #

#: Dimension codes that always carry measure information.
MEASURE_DIM_CODES: frozenset[str] = frozenset(
    {
        "INDICATOR",
        "UNIT_MEASURE",
        "OLCU_BIRIMI",
        "MEASURE",
        "DEGISIM",
        "DEGISIM_TUR",
        "ENDEKS_DEGISIM",
    }
)

#: A code containing this fragment is a measure indicator (ADNKS_GOSTERGE, ...).
MEASURE_DIM_FRAGMENT = "gosterge"

#: Measure dimensions that CARRY the measurement type. Their code labels are
#: evaluated first (tier A); the remaining measure dims (INDICATOR, *GOSTERGE*,
#: SERIE) are tier B; the dataset name is tier C and only feeds a small set of
#: rules, so a dataset title can never masquerade as a weight/contribution.
TYPE_BEARING_DIM_CODES: frozenset[str] = frozenset(
    {
        "DEGISIM",
        "DEGISIM_TUR",
        "ENDEKS_DEGISIM",
        "UNIT_MEASURE",
        "OLCU_BIRIMI",
        "MEASURE",
    }
)

# Breakdown dimensions (never measure). Prefixes, suffixes and exact names kept
# in one constant so the list is auditable; matching is done on folded codes.
_BREAKDOWN_EXACT: frozenset[str] = frozenset(
    {
        "SEX",
        "CINSIYET",
        "ULKE",
        "ARAC_TUR",
        "LEVEL",
        "BASE_PER",
        "BAZ_YILI",
        "YAYIM_DONEMI",
        "SEASONAL_ADJUST",
        "AY",
    }
)
_BREAKDOWN_CONTAINS: tuple[str, ...] = (
    "yas",
    "nace",
    "sekt",
    "buyukluk",
    "egitim",
    "ikamet",
    "duzey",
    "bolge",
    "faaliyet",
)
_BREAKDOWN_PREFIXES: tuple[str, ...] = ("activity", "coicop", "sitc")
_BREAKDOWN_SUFFIXES: tuple[str, ...] = ("cpa",)

_MEASURE_ROLES = frozenset({"time", "geo", "frequency"})


def is_breakdown_dimension(code: str) -> bool:
    folded = fold(code)
    if folded in {token.casefold() for token in _BREAKDOWN_EXACT}:
        return True
    if any(token in folded for token in _BREAKDOWN_CONTAINS):
        return True
    if folded.startswith(_BREAKDOWN_PREFIXES):
        return True
    return folded.endswith(_BREAKDOWN_SUFFIXES)


def classify_dimension(dimension: DimensionView, institution_code: str) -> bool | None:
    """Decide whether a dimension is a measure dimension (``None`` = undecided).

    TCMB/HMB's single ``SERIE`` dimension is always a measure. Roles time/geo/
    frequency are never measure; known breakdown codes are never measure; the
    explicit measure codes and ``*GOSTERGE*`` indicators are measure. Anything
    else is left undecided for Jev.
    """
    if institution_code in {"tcmb", "hmb"} and dimension.code == "SERIE":
        return True
    if dimension.role in _MEASURE_ROLES:
        return False
    folded = fold(dimension.code)
    if dimension.code in MEASURE_DIM_CODES or MEASURE_DIM_FRAGMENT in folded:
        return True
    if is_breakdown_dimension(dimension.code):
        return False
    return None


# --------------------------------------------------------------------------- #
# Combination enumeration
# --------------------------------------------------------------------------- #

MAX_CODES_PER_MEASURE_DIM = 255
MAX_COMBINATIONS = 500


def _product(counts: list[int]) -> int:
    product = 1
    for count in counts:
        product *= count
    return product


def enumerate_combinations(
    measure_dims: list[DimensionView],
) -> tuple[list[dict[str, str]], str | None]:
    """Enumerate the cartesian product of the decided measure dims' codes.

    Returns ``(combinations, note)``. Each combination is a ``{dim: code}`` map
    with sorted keys. ``note`` is set (and the list empty) when the product is
    too large to enumerate; the caller then leaves the dataset for review.
    """
    if not measure_dims:
        return [{}], None
    # A dataset whose only measure dimension is the TCMB/HMB ``SERIE`` dimension
    # has one series per code, not a cartesian product: enumerate it fully even
    # when it holds thousands of codes. The limits below only bound products of
    # several measure dimensions.
    if len(measure_dims) == 1 and measure_dims[0].code == "SERIE":
        dimension = measure_dims[0]
        return [{dimension.code: code.code} for code in dimension.codes], None
    counts = [len(dimension.codes) for dimension in measure_dims]
    if any(count > MAX_CODES_PER_MEASURE_DIM for count in counts):
        return [], "measure dim exceeds code limit"
    if _product(counts) > MAX_COMBINATIONS:
        return [], "measure combination product exceeds limit"

    combinations: list[dict[str, str]] = [{}]
    for dimension in measure_dims:
        grown: list[dict[str, str]] = []
        for base in combinations:
            for code in dimension.codes:
                item = dict(base)
                item[dimension.code] = code.code
                grown.append(item)
        combinations = grown
    return [dict(sorted(item.items())) for item in combinations], None


def combination_label(codes: dict[str, str], dimensions: dict[str, DimensionView]) -> str:
    """Human label: code labels joined with `` | `` in dimension position order.

    The ``codes`` JSON keys stay sorted; only the label follows the dataset's
    dimension order so the indicator (position 0) comes before a change dimension.
    """
    ordered = sorted(
        codes,
        key=lambda dim_code: (
            dimensions[dim_code].position if dim_code in dimensions else 0,
            dim_code,
        ),
    )
    labels: list[str] = []
    for dim_code in ordered:
        dimension = dimensions.get(dim_code)
        if dimension is None:
            labels.append(codes[dim_code])
            continue
        label = next((c.label for c in dimension.codes if c.code == codes[dim_code]), None)
        labels.append(label or codes[dim_code])
    return " | ".join(labels)


# --------------------------------------------------------------------------- #
# Measure type rules
# --------------------------------------------------------------------------- #

# TCMB survey answer options are always numbered ("1. (ARTTI) ...",
# "24. (1-3 AY) ...", "18.6. (Diğer Finansman Kaynakları)_ ..."); the leading
# question number is required so a "(TN) ..." or "(2A) ..." series is not taken
# for an answer option. Anything (digits, hyphens, spaces, Turkish letters) may
# sit inside the parentheses.
_ANSWER_OPTION_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.\s*\([^)]*\)")
_BASE_YEAR_RE = re.compile(r"\(\s*\d{4}\s*=\s*100\s*\)")
_TRAILING_PAREN_RE = re.compile(r"\([^)]*\)\s*$")
_CHANGE_WORDS = ("degisim", "change", "rate")


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _has_word(text: str, *words: str) -> bool:
    for word in words:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text):
            return True
    return False


@dataclass(frozen=True)
class TypeContext:
    """Folded text pieces a measure-type rule may inspect.

    ``tier_a`` holds the type-bearing dimension labels + unit text (empty when
    the combination has no type-bearing dims, so the unit alone never forms tier
    A), ``tier_b`` the other measure-dimension labels + unit text, and
    ``name_fold`` the dataset name. ``tier_a_label``/``tier_b_label`` are the
    labels without the unit so the generic percent/ratio rule cannot fire from a
    unit such as "Oran". Rules run A then B, a restricted name-only pass (tier C),
    then a unit-only generic fallback (tier D).
    """

    label_fold: str
    unit_fold: str
    name_fold: str
    text: str
    category_fold: str
    external_code: str
    is_pka: bool
    is_dibs: bool
    tier_a: str = ""
    tier_b: str = ""
    tier_a_label: str = ""
    tier_b_label: str = ""
    is_survey: bool = False


def _looks_like_pka(view: DatasetView, text: str) -> bool:
    return (
        view.external_code.startswith("bie_pka")
        or "piyasa katilimcilari" in text
        or "piyasa katilimcilari" in fold(view.source_category)
    )


#: Currency tokens that, followed by "/", mean "per physical unit" (a price).
_PER_UNIT_TOKENS = (
    "tl/",
    "try/",
    "usd/",
    "eur/",
    "euro/",
    "avro/",
    "gbp/",
    "jpy/",
    "cny/",
    "chf/",
    "rub/",
    "$/",
    "\u20ac/",
)


def _per_unit_price(text: str) -> bool:
    """True when ``text`` names a currency per physical unit (TRY/HD, TL/kg, ...).

    A cross-rate pair ``(USD/LUF)`` is NOT a per-unit price, so it is excluded.
    """
    if _EXCHANGE_PAIR_RE.search(text):
        return False
    return _has(text, *_PER_UNIT_TOKENS)


#: A cross-rate pair such as ``(USD/LUF)``; the first code is the quote currency.
_EXCHANGE_PAIR_RE = re.compile(r"\(\s*([A-Za-z]{3})\s*/\s*([A-Za-z]{3})\s*\)")


def exchange_quote_currency(label: str) -> str:
    """The quote currency of a TCMB exchange-rate series.

    TCMB quotes foreign currencies in TL, so a plain rate is TRY. A cross pair
    ``A/B`` means "units of B per one A", so the quote is its SECOND code; a
    cross with no pair defaults to USD (TCMB cross rates are against USD).
    """
    match = _EXCHANGE_PAIR_RE.search(label)
    if match:
        return normalize_currency(match.group(2).upper())
    if "capraz" in fold(label):
        return "USD"
    return "TRY"


def _strip_trailing_parens(label: str) -> str:
    """Strip every trailing parenthesised group.

    DİBS labels carry several: ``... Değer (24D2K4211118) (Arşiv)`` -> the
    benchmark word is only exposed after all of them are removed.
    """
    cleaned = label.strip()
    while True:
        stripped = _TRAILING_PAREN_RE.sub("", cleaned).strip()
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def _precise_unit_rules(context: TypeContext) -> tuple[str | None, str | None, str | None]:
    """Early precise-unit rules; they run before every label rule."""
    unit = context.unit_fold
    if "net yuzde degisim" in unit:
        return "denge_yayilma", "net_change_balance", "D"
    if _per_unit_price(unit):
        return "fiyat_kur", "unit_per_unit_price", "D"
    if _per_unit_price(context.tier_a_label) or _per_unit_price(context.tier_b_label):
        return "fiyat_kur", "unit_per_unit_price", "B"
    if "endeks" in unit:
        return "endeks", "unit_index", "D"
    return None, None, None


def _participant_phrase(label: str, *, is_survey: bool) -> bool:
    if _has(label, "katilimci sayisi", "number of participants", "number of respondents"):
        return True
    if _has(
        label,
        "cevap veren firma sayisi",
        "cevap veren isyeri sayisi",
        "cevap veren sayisi",
        "yanit veren firma sayisi",
        "yanit veren isyeri sayisi",
        "yanit veren sayisi",
    ):
        return True
    return is_survey and ("firma sayisi" in label or "isyeri sayisi" in label)


def _contribution_phrase(label: str) -> bool:
    return _has(
        label,
        "contribution to",
        "contributions to",
        "katkisi",
        "puan katki",
        "percentage point",
    )


def _label_match(context: TypeContext, predicate: Any) -> str | None:
    """Return the tier ("A"/"B") whose label text satisfies ``predicate``."""
    for tier, label in (("A", context.tier_a_label), ("B", context.tier_b_label)):
        if label and predicate(label):
            return tier
    return None


def _tier_rules(context: TypeContext) -> tuple[str | None, str | None]:
    """The rule set run against one label tier (A and B), after the pre-passes."""
    text = context.text
    label = context.label_fold
    unit = context.unit_fold

    if _has(
        text,
        "monthly rate of change",
        "change compared to the previous month",
        "aylik degisim",
        "bir onceki aya gore",
    ):
        return "aylik_yuzde_degisim", "percent_change_monthly"
    if _has(text, "twelve months moving average", "12 aylik ortalama"):
        return "on_iki_ay_ort_yuzde_degisim", "percent_change_12m_avg"
    if _has(text, "december of the previous year", "onceki yilin aralik", "yilbasindan"):
        return "yilbasindan_yuzde_degisim", "percent_change_ytd"
    if _has(
        text,
        "annual rate of change",
        "annual change",
        "same month of the previous year",
        "same quarter of the previous year",
        "yillik degisim",
        "onceki yilin ayni",
    ):
        return "yillik_yuzde_degisim", "percent_change_annual"
    if _has(text, "previous quarter", "quarterly change", "onceki ceyrege gore"):
        return "donemsel_yuzde_degisim", "percent_change_period"
    if _has_word(text, "weight", "weights", "agirlik") and _has(
        text,
        "%",
        "weights of",
        "expenditure",
        "harcama",
        "sepet",
        "basket",
        "endeks agirlik",
    ):
        return "agirlik", "weight"
    if _index_rule(context):
        return "endeks", "index"
    if _has(text, "per capita", "kisi basina"):
        return "kisi_basina", "per_capita"
    if _has(text, "\u2030", "per thousand", "per 100 000", "per 100000", "yuz binde") or _has_word(
        text, "binde"
    ):
        return "hiz_binde", "rate_per_thousand"
    if _ANSWER_OPTION_RE.match(label) and "yuzde" in unit:
        return "cevap_dagilimi", "survey_answer_option"
    if context.is_pka and _has(
        text,
        "standart sapma",
        "standard deviation",
        "en buyuk",
        "en kucuk",
        "en yuksek",
        "en dusuk",
        "medyan",
        "median",
        "minimum",
        "maximum",
    ):
        return "dagilim_istatistigi", "pka_distribution_stat"
    if _has(text, "faiz orani", "interest rate", "kupon faiz"):
        return "oran_pay", "interest_rate"
    if context.is_dibs and _strip_trailing_parens(label).endswith(("deger", "diger")):
        return "fiyat_kur", "dibs_benchmark_value"
    if "reel efektif doviz kuru" in text:
        return "endeks", "real_effective_exchange_index"
    if _has(text, "doviz alis", "doviz satis", "efektif alis", "efektif satis", "(usd)") or (
        "kurlar" in context.category_fold and "reel efektif" not in text
    ):
        return "fiyat_kur", "exchange_rate"
    # The generic percent/ratio rule must judge the labels, never the unit; a
    # unit "Oran" alone is handled by the tier-D unit fallback.
    if _has(label, "(%)", "orani") or _has_word(
        label, "percent", "ratio", "proportion", "oran", "share", "agirlik", "pay"
    ):
        return "oran_pay", "percent_ratio_share"
    return None, None


def _unit_rules(context: TypeContext) -> tuple[str | None, str | None]:
    """Tier D: the generic rules that fire purely because of the unit (run last)."""
    unit = context.unit_fold
    if not unit:
        return None, None
    if _has(unit, "yuzde", "(%)", "%") or _has_word(unit, "oran"):
        return "oran_pay", "unit_percent"
    return None, None


def _name_rules(context: TypeContext) -> tuple[str | None, str | None]:
    """The restricted tier-C rule set (dataset name only)."""
    text = context.text
    # A change label ("Percentage change", "değişim", ...) means the name's
    # "index" is the base series behind a change measure, not the measure itself.
    labels_say_change = _has(context.label_fold, "change", "degisim")
    if not labels_say_change:
        if _BASE_YEAR_RE.search(text) and not _has(text, *_CHANGE_WORDS):
            return "endeks", "index_name"
        if _has_word(text, "endeks", "index"):
            return "endeks", "index_name"
    if _has(text, "oranlari", "ratios", "rates"):
        return "oran_pay", "percent_ratio_name"
    if _has(text, "doviz alis", "doviz satis", "efektif alis", "efektif satis", "(usd)") or (
        "kurlar" in context.category_fold and "reel efektif" not in text
    ):
        return "fiyat_kur", "exchange_rate"
    if context.is_pka and _has(
        text,
        "standart sapma",
        "standard deviation",
        "en yuksek",
        "en dusuk",
        "medyan",
        "median",
        "minimum",
        "maximum",
    ):
        return "dagilim_istatistigi", "pka_distribution_stat"
    if context.is_dibs and _strip_trailing_parens(context.label_fold).endswith(("deger", "diger")):
        return "fiyat_kur", "dibs_benchmark_value"
    return None, None


def evaluate_type_rules(context: TypeContext) -> tuple[str | None, str | None, str | None]:
    """Evaluate the type rules in order; return ``(type_id, rule, tier)``.

    Order: precise unit rules (net yüzde değişim, per-unit price, unit "Endeks")
    -> participant count -> strict contribution -> tiers A/B/C (the remaining
    label/name rules) -> tier D (the generic unit fallback).
    """
    # 1. Precise unit rules, before anything that reads a label.
    result = _precise_unit_rules(context)
    if result[0] is not None:
        return result[0], result[1], result[2]
    # 2. Participant count, before any percent/unit rule.
    tier = _label_match(
        context, lambda label: _participant_phrase(label, is_survey=context.is_survey)
    )
    if tier is not None:
        return "katilimci_sayisi", "participant_count", tier
    # 3. Contribution, before the percent-change rules.
    tier = _label_match(context, _contribution_phrase)
    if tier is not None:
        return "katki_puan", "contribution", tier
    # 4. Tiers A (type-bearing labels) and B (other measure labels).
    if context.tier_a:
        tier_a = replace(
            context,
            text=context.tier_a,
            label_fold=context.tier_a_label or context.tier_a,
        )
        result = _tier_rules(tier_a)
        if result[0] is not None:
            return result[0], result[1], "A"
    if context.tier_b:
        tier_b = replace(
            context,
            text=context.tier_b,
            label_fold=context.tier_b_label or context.tier_b,
        )
        result = _tier_rules(tier_b)
        if result[0] is not None:
            return result[0], result[1], "B"
    # 5. Tier C (dataset name, restricted).
    tier_c = replace(
        context,
        text=context.name_fold,
        label_fold=context.label_fold,
        unit_fold="",
    )
    result = _name_rules(tier_c)
    if result[0] is not None:
        return result[0], result[1], "C"
    # 6. Tier D (generic unit fallback).
    result = _unit_rules(context)
    if result[0] is not None:
        return result[0], result[1], "D"
    return None, None, None


def measure_type_rule(context: TypeContext) -> tuple[str | None, str | None]:
    """Backward-compatible ``(measure_type_id, rule_name)`` wrapper."""
    measure_type, rule, _tier = evaluate_type_rules(context)
    return measure_type, rule


def _index_rule(context: TypeContext) -> bool:
    # The unit "Endeks" case is a precise-unit pre-pass, not a label rule.
    if context.label_fold.startswith(("index", "endeks")):
        return True
    if _BASE_YEAR_RE.search(context.text) and not _has(context.text, *_CHANGE_WORDS):
        return True
    return False


# --------------------------------------------------------------------------- #
# Data nature rules
# --------------------------------------------------------------------------- #

_SURVEY_MARKERS = ("beklenti ve egilim anket", "anket", "survey")
_PROJECTION_WORDS = ("projeksiyon", "projection", "tahmin", "forecast")
_EXPECTATION_WORDS = (
    "beklenti",
    "expectation",
    "gelecek",
    "onumuzdeki",
    "next 3 months",
    "next 12 months",
    "cari yil sonu",
    "yil sonu beklentisi",
)
_REALISED_WORDS = ("son uc ay", "gecen", "su anda", "past 3 months", "currently")


def is_survey_dataset(view: DatasetView) -> bool:
    """True when the dataset looks like a survey/expectation questionnaire."""
    haystack = fold(
        " ".join(
            part
            for part in (view.name, view.source_category, _category_path_text(view))
            if part
        )
    )
    return any(marker in haystack for marker in _SURVEY_MARKERS)


def data_nature_rule(view: DatasetView, context: TypeContext) -> tuple[str | None, str | None]:
    """Return ``(data_nature_id, rule_name)``; ``None`` means pending for Jev."""
    text = context.text
    if _has(text, *_PROJECTION_WORDS):
        return "tahmin", "projection"
    if _has(text, *_EXPECTATION_WORDS):
        return "beklenti", "expectation"
    if is_survey_dataset(view):
        if _has(text, *_REALISED_WORDS):
            return "gerceklesen", "survey_realised"
        return None, None
    return "gerceklesen", "default_realised"


# --------------------------------------------------------------------------- #
# Aggregation (effective)
# --------------------------------------------------------------------------- #

_AGGREGATION_LABELS = {
    "toplam": "toplam",
    "ortalama": "ortalama",
    "donem_sonu": "donem_sonu",
    "yeniden_hesapla": "yeniden_hesapla",
    "test_disi": "test_disi",
}


def effective_aggregation(
    measure_type: str | None,
    source_aggregation: str | None,
    tree: ConceptTree,
) -> tuple[str | None, bool]:
    """Resolve the effective aggregation; returns ``(aggregation, conflict)``.

    Source ``sum``/``avg`` win (mapped to ``toplam``/``ortalama``). Source
    ``last``/``max``/``min`` is an untrusted EVDS default: our measure-type rule
    wins and a disagreement is recorded. ``conflict`` is true when the source
    default disagrees with our type default.
    """
    default = tree.aggregation_rule(measure_type)
    source = (source_aggregation or "").strip().lower() or None
    # A conflict is only meaningful when BOTH our type default and the source
    # value are known; without a measure type the source value cannot disagree.
    if default is None:
        if source == "sum":
            return "toplam", False
        if source == "avg":
            return "ortalama", False
        return None, False
    if source == "sum":
        return "toplam", default != "toplam"
    if source == "avg":
        return "ortalama", default != "ortalama"
    if source in {"last", "max", "min"}:
        return default, default != "donem_sonu"
    return default, False


def _is_kumulatif(*texts: str | None) -> bool:
    haystack = fold(" ".join(text for text in texts if text))
    return _has(
        haystack,
        "kumulatif",
        "cumulative",
        "yilbasindan itibaren",
        "year to date",
    )


# --------------------------------------------------------------------------- #
# Currency, nominal/real, seasonal
# --------------------------------------------------------------------------- #

MONETARY_TYPES = frozenset({"akim_tutar", "stok_tutar", "fiyat_kur", "kisi_basina"})

#: The only values ``para_birimi`` may carry. Anything else maps to ``diger``.
ALLOWED_CURRENCIES = frozenset({"TRY", "USD", "EUR", "XDR", "diger"})

_CURRENCY_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TRY", ("turk lirasi", "bin tl", "milyon tl", "thousand try", "thousend try", "tl", "try")),
    ("USD", ("abd dolari", "dolar", "usd", "$")),
    ("EUR", ("euro", "avro", "eur", "\u20ac")),
    ("XDR", ("sdr", "xdr")),
    ("diger", ("sterlin", "gbp", "jpy", "yuan", "cny", "frank", "chf", "ruble", "rub")),
)

# Bare "real"/"volume"/"hacim" are deliberately not matched: they hit
# "real estate" and "trade volume"/"satış hacmi" amounts that are not real terms.
# :func:`nominal_rule` matches the explicit reel/nominal phrases inline.


def normalize_currency(code: str | None) -> str:
    """Return ``code`` when it is an allowed currency, else ``diger``."""
    if code in ALLOWED_CURRENCIES:
        return str(code)
    return "diger"


def detect_currency(text: str) -> str | None:
    """Map an explicit currency mention to a code, or ``None`` when absent."""
    folded = fold(text)
    for code, tokens in _CURRENCY_TOKENS:
        for token in tokens:
            if token in {"$", "\u20ac"}:
                if token in folded:
                    return code
            elif re.search(r"(?<![a-z])" + re.escape(token) + r"(?![a-z])", folded):
                return code
    return None


def currency_rule(
    measure_type: str | None, text: str
) -> tuple[str | None, dict[str, Any]]:
    """Resolve currency for a combination and the attribute markers to store."""
    detected = detect_currency(text)
    if measure_type in MONETARY_TYPES:
        if detected is not None:
            return detected, {"currency": detected, "currency_method": "rule"}
        return None, {"currency_pending": True}
    if measure_type is None:
        # Unknown type: "n/a" would be a claim we cannot make yet.
        if detected is not None:
            return detected, {"currency": detected, "currency_method": "rule"}
        return None, {"currency_pending": True}
    return None, {"currency": "n/a", "currency_method": "rule"}


def nominal_rule(measure_type: str | None, text: str) -> tuple[str | None, dict[str, Any]]:
    """Resolve nominal/real for amount types; attributes carry the reason."""
    if measure_type is None:
        # Unknown type: "n/a" would be a claim we cannot make yet.
        return None, {"nominal_pending": True}
    if measure_type not in {"akim_tutar", "stok_tutar", "kisi_basina"}:
        return None, {"nominal": "n/a", "nominal_method": "rule"}
    folded = fold(text)
    if _has_word(folded, "reel") or _has(
        folded,
        "real terms",
        "in real",
        "constant price",
        "sabit fiyat",
        "zincirlenmis",
        "chain-linked",
        "chained volume",
    ):
        return "reel", {"nominal": "reel", "nominal_method": "rule"}
    if _has_word(folded, "nominal") or _has(folded, "cari fiyat", "current price"):
        return "nominal", {"nominal": "nominal", "nominal_method": "rule"}
    return None, {"nominal_pending": True}


#: Unadjusted phrases that must beat the positive markers below.
_SEASONAL_NEGATIVE_WORDS = (
    "not seasonally adjusted",
    "non-seasonally adjusted",
    "seasonally unadjusted",
    "arindirilmamis",
    "mevsim etkisinden arindirilmamis",
)
_SEASONAL_POSITIVE_WORDS = (
    "mevsim",
    "seasonally adjusted",
    "takvim etkisinden arindirilmis",
    "calendar adjusted",
)


def seasonal_rule(text: str) -> str:
    """Return ``arindirilmis`` or ``ham`` (default) for one combination."""
    folded = fold(text)
    if _has(folded, *_SEASONAL_NEGATIVE_WORDS) or _has_word(folded, "ham"):
        return "ham"
    if _has(folded, *_SEASONAL_POSITIVE_WORDS):
        return "arindirilmis"
    return "ham"


def seasonal_from_dimension(dimension: DimensionView) -> set[str]:
    values: set[str] = set()
    for code in dimension.codes:
        label = fold(code.label)
        if _has(label, *_SEASONAL_NEGATIVE_WORDS) or _has_word(label, "ham"):
            values.add("ham")
        elif _has(label, *_SEASONAL_POSITIVE_WORDS):
            values.add("arindirilmis")
    return values


# --------------------------------------------------------------------------- #
# Dataset flags
# --------------------------------------------------------------------------- #

#: Multi-topic compilations (constant; documented in the task spec).
COMPILATION_CODES: frozenset[str] = frozenset(
    {
        "TURCAT_REEL",
        "TURCAT_MALI",
        "TURCAT_FINANS",
        "TURCAT_DIS",
        "TURCAT_NUFUS",
        "bie_pkauo",
        "bie_urbeka",
    }
)


def _category_path_text(view: DatasetView) -> str:
    """Concatenated category-path titles (folded by callers as needed)."""
    path = view.attributes.get("category_path")
    if not isinstance(path, list):
        return ""
    titles: list[str] = []
    for entry in path:
        if isinstance(entry, dict) and entry.get("title"):
            titles.append(str(entry["title"]))
    return " ".join(titles)


def is_arsiv(view: DatasetView) -> bool:
    """Archive flag: only the source's own ``Arşiv`` labelling (decision 7)."""
    if "arsiv" in fold(view.name):
        return True
    path = view.attributes.get("category_path")
    if isinstance(path, list) and path:
        first = path[0]
        if isinstance(first, dict) and fold(str(first.get("title", ""))) == "arsiv":
            return True
    if "(arsiv)" in fold(view.source_category):
        return True
    return False


def is_revizyon_tablosu(view: DatasetView) -> bool:
    haystack = fold(view.name)
    return _has(haystack, "revision history", "revizyon gecmisi", "revizyon tablosu")


def is_cok_konulu_derleme(view: DatasetView) -> bool:
    return view.external_code in COMPILATION_CODES


# --------------------------------------------------------------------------- #
# Period-series candidate grouping
# --------------------------------------------------------------------------- #

_PERIOD_PATTERNS: tuple[str, ...] = (
    r"\(\s*\d{4}\s*[-\u2013]\s*\d{4}\s*\)",
    r"\b\d{4}\s*[-\u2013]\s*\d{4}\b",
    r"\(\s*\d{4}\s+and\s+(?:earlier|onwards|later)\s*\)",
    r"\b\d{4}\s+ve\s+(?:oncesi|sonrasi)\b",
    r"\(\s*\d{4}\s*=\s*100\s*\)",
    r"\(\s*arsiv\s*\)",
    r"\barsiv\b",
    r"\(\s*eski\s*\)",
    r"\beski\b",
    r"[_\-]?v(?:ersiyon|ersion)?\s*\d+\b",
)


def normalize_period_name(name: str) -> tuple[str, bool]:
    """Strip period/base-year/archive markers; return ``(normalised, had_marker)``."""
    folded = fold(name)
    had_marker = False
    for pattern in _PERIOD_PATTERNS:
        new, count = re.subn(pattern, " ", folded)
        if count:
            had_marker = True
            folded = new
    cleaned = re.sub(r"[^a-z0-9]+", " ", folded)
    return re.sub(r"\s+", " ", cleaned).strip(), had_marker


def period_series_candidates(
    datasets: list[tuple[str, str, str]],
) -> dict[tuple[str, str], str]:
    """Group same-institution datasets by normalised name.

    ``datasets`` is ``(institution_code, external_code, name)``. A group is a
    candidate when it has at least two members and at least one member lost a
    period/base-year marker during normalisation. Returns a map from
    ``(institution_code, external_code)`` to the shared group key.
    """
    groups: dict[tuple[str, str], list[tuple[tuple[str, str], bool]]] = {}
    for institution_code, external_code, name in datasets:
        normalized, had_marker = normalize_period_name(name)
        group_id = (institution_code, normalized)
        groups.setdefault(group_id, []).append(((institution_code, external_code), had_marker))
    candidates: dict[tuple[str, str], str] = {}
    for (institution_code, normalized), members in groups.items():
        if len(members) < 2 or not any(had_marker for _, had_marker in members):
            continue
        key = f"{institution_code}::{normalized}"
        for identity, _ in members:
            candidates[identity] = key
    return candidates


# --------------------------------------------------------------------------- #
# Description template
# --------------------------------------------------------------------------- #

_FREQUENCY_TR = {
    "daily": "günlük",
    "weekly": "haftalık",
    "monthly": "aylık",
    "quarterly": "çeyreklik",
    "semiannual": "altı aylık",
    "annual": "yıllık",
    "irregular": "düzensiz",
}
_FREQUENCY_TR_FOLDED = {fold(key): value for key, value in _FREQUENCY_TR.items()}
_FREQUENCY_TR_REVERSE = {fold(value): value for value in _FREQUENCY_TR.values()}


def turkish_frequency(value: str | None) -> str | None:
    """Translate a canonical or Turkish frequency value to its Turkish label."""
    if not value:
        return None
    folded = fold(value)
    if folded in _FREQUENCY_TR_FOLDED:
        return _FREQUENCY_TR_FOLDED[folded]
    return _FREQUENCY_TR_REVERSE.get(folded)


def _distinct(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def dataset_frequencies(view: DatasetView) -> list[str]:
    """Distinct Turkish frequency labels for the description template."""
    raw: list[str | None] = []
    for dimension in view.dimensions:
        if dimension.code == "FREQ":
            raw.extend(code.label for code in dimension.codes)
        for code in dimension.codes:
            frequency = code.attributes.get("frequency")
            if frequency:
                raw.append(str(frequency))
    default = view.attributes.get("default_frequency")
    if default:
        raw.append(str(default))
    return _distinct([turkish_frequency(value) for value in raw])


def dataset_units(view: DatasetView) -> list[str]:
    """Distinct unit labels for the description template (source order)."""
    raw: list[str | None] = []
    for dimension in view.dimensions:
        if dimension.code in {"UNIT_MEASURE", "OLCU_BIRIMI"}:
            raw.extend(code.label for code in dimension.codes)
        for code in dimension.codes:
            unit = code.attributes.get("unit")
            if unit:
                raw.append(str(unit))
    for key in ("unit_hint", "unit"):
        value = view.attributes.get(key)
        if value:
            raw.append(str(value))
    return _distinct(raw)


def format_units(units: list[str], *, limit: int = 3) -> str | None:
    """Join up to ``limit`` units, appending ``…`` when more exist."""
    if not units:
        return None
    if len(units) <= limit:
        return ", ".join(units)
    return ", ".join(units[:limit]) + ", …"


def dataset_description(
    institution_name: str,
    view: DatasetView,
    *,
    frequencies: list[str] | None = None,
    units: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if institution_name:
        parts.append(institution_name)
    if view.source_category:
        parts.append(view.source_category)
    freq_text = ", ".join(frequencies if frequencies is not None else dataset_frequencies(view))
    if freq_text:
        parts.append(freq_text)
    units_text = format_units(units if units is not None else dataset_units(view))
    if units_text:
        parts.append(units_text)
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# Per-dataset plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CombinationPlan:
    """One planned ``measure_combinations`` row."""

    codes: dict[str, str]
    label: str
    measure_type: str | None
    data_nature: str | None
    aggregation: str | None
    source_aggregation: str | None
    aggregation_conflict: bool
    para_birimi: str | None
    nominal_mi: str | None
    mevsim_arindirilmis: str | None
    kumulatif: bool
    type_method: str | None
    nature_method: str | None
    type_confidence: float | None
    nature_confidence: float | None
    attributes: dict[str, Any]


@dataclass(frozen=True)
class DatasetPlan:
    """The full plan for one dataset (description, flags, combinations)."""

    description: str
    source_description: str | None
    revizyon_tablosu: bool
    arsiv: bool
    cok_konulu_derleme: bool
    mevsim_arindirilmis: tuple[str, ...]
    para_birimi: tuple[str, ...]
    nominal_mi: tuple[str, ...]
    measure_dims: dict[str, bool | None]
    combinations: tuple[CombinationPlan, ...]
    attributes: dict[str, Any]


def _combination_context(
    view: DatasetView,
    label: str,
    code_views: list[CodeView],
    codes: dict[str, str],
) -> TypeContext:
    unit_parts: list[str] = []
    for code in code_views:
        unit = code.attributes.get("unit")
        if unit:
            unit_parts.append(str(unit))
    for key in ("unit", "unit_hint"):
        value = view.attributes.get(key)
        if value:
            unit_parts.append(str(value))

    dimensions = {dimension.code: dimension for dimension in view.dimensions}
    tier_a_labels: list[str] = []
    tier_b_labels: list[str] = []
    if not codes:
        # No measure dimension: the dataset name IS the indicator (CİP and other
        # single-indicator tables), so the full rule set judges it as tier B.
        tier_b_labels.append(view.name)
    else:
        for dimension_code, code_value in codes.items():
            dimension = dimensions.get(dimension_code)
            row = (
                next((code for code in dimension.codes if code.code == code_value), None)
                if dimension is not None
                else None
            )
            if (
                dimension is not None
                and dimension.code in TYPE_BEARING_DIM_CODES
                and row is not None
            ):
                tier_a_labels.append(row.label)
                if dimension.code in {"UNIT_MEASURE", "OLCU_BIRIMI"}:
                    unit_parts.append(row.label)
            elif row is not None:
                tier_b_labels.append(row.label)
            else:
                tier_b_labels.append(code_value)

    unit_text = " | ".join(_distinct(unit_parts))
    # Tier A exists only when a type-bearing dimension contributes a label; the
    # unit alone must never form tier A (it would pre-empt the tier-B rules).
    tier_a_label = fold(" | ".join(tier_a_labels))
    tier_b_label = fold(" | ".join(tier_b_labels))
    tier_a_text = (
        fold(" | ".join(part for part in (" | ".join(tier_a_labels), unit_text) if part))
        if tier_a_labels
        else ""
    )
    tier_b_text = fold(" | ".join(part for part in (" | ".join(tier_b_labels), unit_text) if part))
    text = fold(" | ".join(part for part in (label, unit_text, view.name) if part))
    category_fold = fold(
        " ".join(
            part for part in (view.source_category, _category_path_text(view)) if part
        )
    )
    return TypeContext(
        label_fold=fold(label),
        unit_fold=fold(unit_text),
        name_fold=fold(view.name),
        text=text,
        category_fold=category_fold,
        external_code=view.external_code,
        is_pka=_looks_like_pka(view, text),
        is_dibs=view.external_code.startswith("bie_pydibs"),
        tier_a=tier_a_text,
        tier_b=tier_b_text,
        tier_a_label=tier_a_label,
        tier_b_label=tier_b_label,
        is_survey=is_survey_dataset(view),
    )


def derived_combination(
    view: DatasetView,
    tree: ConceptTree,
    codes: dict[str, str],
    label: str,
    code_views: list[CodeView],
    *,
    measure_type: str | None,
    data_nature: str | None,
    type_method: str | None,
    nature_method: str | None,
    type_confidence: float | None,
    nature_confidence: float | None,
    type_rule: str | None = None,
    type_rule_tier: str | None = None,
    nature_rule: str | None = None,
) -> CombinationPlan:
    """Build a combination from an already-known measure type/nature.

    All the dependent rules (aggregation, conflict, currency, nominal, seasonal,
    cumulative) reuse the 2.2a functions. :func:`compute_combination` calls this
    with the rule results; the Jev pass calls it with Jev's answer so the derived
    fields are recomputed for the decided type.
    """
    context = _combination_context(view, label, code_views, codes)
    source_aggregation = next(
        (
            str(code.attributes["aggregation"])
            for code in code_views
            if code.attributes.get("aggregation")
        ),
        None,
    )
    aggregation, conflict = effective_aggregation(measure_type, source_aggregation, tree)

    currency_text = f"{label} {context.unit_fold}"
    para_birimi, currency_attributes = currency_rule(measure_type, currency_text)
    if type_rule == "exchange_rate":
        # An exchange rate's series currency is its QUOTE currency, not the
        # currency code in the label (TCMB quotes foreign in TL).
        quote = exchange_quote_currency(label)
        para_birimi = quote
        currency_attributes = {
            "currency": quote,
            "currency_method": "rule",
            "currency_confidence": 1.0,
        }
    elif type_rule == "dibs_benchmark_value":
        # A DİBS benchmark VALUE is a bond price per 100 nominal (a percent of
        # par), not a currency amount: currency is not applicable by rule.
        para_birimi = None
        currency_attributes = {
            "currency": "n/a",
            "currency_method": "rule",
            "currency_confidence": 1.0,
        }
    nominal_mi, nominal_attributes = nominal_rule(measure_type, currency_text)
    mevsim = seasonal_rule(f"{label} | {view.name}")
    kumulatif = _is_kumulatif(view.name, label)

    attributes: dict[str, Any] = {}
    if type_rule:
        attributes["type_rule"] = type_rule
    if type_rule_tier:
        attributes["type_rule_tier"] = type_rule_tier
    if nature_rule:
        attributes["nature_rule"] = nature_rule
    attributes.update(currency_attributes)
    attributes.update(nominal_attributes)

    return CombinationPlan(
        codes=dict(sorted(codes.items())),
        label=label,
        measure_type=measure_type,
        data_nature=data_nature,
        aggregation=aggregation,
        source_aggregation=source_aggregation,
        aggregation_conflict=conflict,
        para_birimi=para_birimi,
        nominal_mi=nominal_mi,
        mevsim_arindirilmis=mevsim,
        kumulatif=kumulatif,
        type_method=type_method,
        nature_method=nature_method,
        type_confidence=type_confidence,
        nature_confidence=nature_confidence,
        attributes=attributes,
    )


def compute_combination(
    view: DatasetView,
    tree: ConceptTree,
    codes: dict[str, str],
    label: str,
    code_views: list[CodeView],
) -> CombinationPlan:
    """Compute one combination row from its label, codes and dataset view."""
    context = _combination_context(view, label, code_views, codes)
    measure_type, type_rule, type_rule_tier = evaluate_type_rules(context)
    data_nature, nature_rule = data_nature_rule(view, context)
    return derived_combination(
        view,
        tree,
        codes,
        label,
        code_views,
        measure_type=measure_type,
        data_nature=data_nature,
        type_method="rule" if measure_type else None,
        nature_method="rule" if data_nature else None,
        type_confidence=1.0 if measure_type else None,
        nature_confidence=1.0 if data_nature else None,
        type_rule=type_rule,
        type_rule_tier=type_rule_tier,
        nature_rule=nature_rule,
    )


def compute_dataset_plan(view: DatasetView, tree: ConceptTree) -> DatasetPlan:
    """Compute the whole dataset plan (pure; the CLI persists it)."""
    dimensions = {dimension.code: dimension for dimension in view.dimensions}
    measure_dims: dict[str, bool | None] = {}
    for dimension in view.dimensions:
        if dimension.is_measure is not None:
            measure_dims[dimension.code] = dimension.is_measure
        else:
            measure_dims[dimension.code] = classify_dimension(dimension, view.institution_code)

    decided = [
        dimension for dimension in view.dimensions if measure_dims.get(dimension.code) is True
    ]
    undecided = any(value is None for value in measure_dims.values())
    combinations, note = enumerate_combinations(decided)

    code_index = {
        dimension.code: {code.code: code for code in dimension.codes}
        for dimension in view.dimensions
    }
    plans: list[CombinationPlan] = []
    for codes in combinations:
        if codes:
            label = combination_label(codes, dimensions)
            code_views = [code_index[dim_code][code] for dim_code, code in codes.items()]
        else:
            label = view.name
            code_views = []
        plans.append(compute_combination(view, tree, codes, label, code_views))

    currencies = tuple(sorted({p.para_birimi for p in plans if p.para_birimi}))
    nominal = tuple(sorted({p.nominal_mi for p in plans if p.nominal_mi}))
    seasonal: set[str] = {p.mevsim_arindirilmis for p in plans if p.mevsim_arindirilmis}
    for dimension in view.dimensions:
        if "SEASONAL_ADJUST" in dimension.code.upper() or "SEASONAL_ADJUST" in fold(dimension.code):
            seasonal |= seasonal_from_dimension(dimension)

    attributes: dict[str, Any] = {}
    if note:
        attributes["measure_enrich_note"] = note
    if undecided:
        attributes["dims_pending"] = True

    # The key is written by the first run (possibly as null) so a later run never
    # mistakes our own description template for a source description. Moving
    # happens only while the key is ABSENT.
    source_description = None
    if "source_description" not in view.attributes:
        source_description = view.description

    return DatasetPlan(
        description=dataset_description(view.institution_name, view),
        source_description=source_description,
        revizyon_tablosu=is_revizyon_tablosu(view),
        arsiv=is_arsiv(view),
        cok_konulu_derleme=is_cok_konulu_derleme(view),
        mevsim_arindirilmis=tuple(sorted(seasonal)),
        para_birimi=currencies,
        nominal_mi=nominal,
        measure_dims=measure_dims,
        combinations=tuple(plans),
        attributes=attributes,
    )


__all__ = [
    "ALLOWED_CURRENCIES",
    "COMPILATION_CODES",
    "MAX_CODES_PER_MEASURE_DIM",
    "MAX_COMBINATIONS",
    "MEASURE_DIM_CODES",
    "MONETARY_TYPES",
    "TYPE_BEARING_DIM_CODES",
    "CodeView",
    "CombinationPlan",
    "DatasetPlan",
    "DatasetView",
    "DimensionView",
    "TypeContext",
    "classify_dimension",
    "combination_label",
    "compute_combination",
    "compute_dataset_plan",
    "currency_rule",
    "data_nature_rule",
    "dataset_description",
    "dataset_frequencies",
    "dataset_units",
    "derived_combination",
    "detect_currency",
    "effective_aggregation",
    "exchange_quote_currency",
    "enumerate_combinations",
    "evaluate_type_rules",
    "fold",
    "format_units",
    "is_arsiv",
    "is_breakdown_dimension",
    "is_cok_konulu_derleme",
    "is_revizyon_tablosu",
    "is_survey_dataset",
    "measure_type_rule",
    "nominal_rule",
    "normalize_currency",
    "normalize_period_name",
    "period_series_candidates",
    "seasonal_from_dimension",
    "seasonal_rule",
    "turkish_frequency",
]
