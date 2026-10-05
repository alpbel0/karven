"""Unit tests for the deterministic enrichment rules (no database, no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog import enrich_rules as rules
from app.catalog.tree import load_tree


def _ctx(
    label: str,
    unit: str = "",
    name: str = "",
    category: str = "",
    external_code: str = "x",
) -> rules.TypeContext:
    joined = " | ".join(part for part in (label, unit, name) if part)
    survey_text = rules.fold(name + category)
    return rules.TypeContext(
        label_fold=rules.fold(label),
        unit_fold=rules.fold(unit),
        name_fold=rules.fold(name),
        text=rules.fold(joined),
        category_fold=rules.fold(category),
        external_code=external_code,
        is_pka="piyasa katilimcilari" in survey_text or external_code.startswith("bie_pka"),
        is_dibs=external_code.startswith("bie_pydibs"),
        tier_a="",
        tier_b=rules.fold(" | ".join(part for part in (label, unit) if part)),
        tier_a_label="",
        tier_b_label=rules.fold(label),
        is_survey="anket" in survey_text or "survey" in survey_text,
    )


def _survey_view(name: str, category: str = "BEKLENTİ VE EĞİLİM ANKETİ") -> rules.DatasetView:
    return rules.DatasetView(
        institution_code="tcmb",
        institution_name="Türkiye Cumhuriyet Merkez Bankası",
        external_code="bie_pka",
        name=name,
        source_category=category,
    )


# --------------------------------------------------------------------------- #
# Measure type
# --------------------------------------------------------------------------- #

MEASURE_TYPE_CASES = [
    ("Index", "", "endeks"),
    ("Percent", "", "oran_pay"),
    ("Pure number", "", None),
    ("Annual rate of change (%)", "", "yillik_yuzde_degisim"),
    ("Change compared to the previous month (%)", "", "aylik_yuzde_degisim"),
    ("Rate of change in twelve months moving averages (%)", "", "on_iki_ay_ort_yuzde_degisim"),
    ("Rate of change on december of the previous year (%)", "", "yilbasindan_yuzde_degisim"),
    ("Monthly rate of change (%)", "", "aylik_yuzde_degisim"),
    ("Weights of expenditure groups (%)", "", "agirlik"),
    ("Thousand Person", "", None),
    ("Tonnes", "", None),
    ("Million $", "", None),
    ("Thousend TRY", "", None),
]


@pytest.mark.parametrize("label,unit,expected", MEASURE_TYPE_CASES)
def test_measure_type_rules(label: str, unit: str, expected: str | None) -> None:
    measure_type, _rule = rules.measure_type_rule(_ctx(label, unit))
    assert measure_type == expected, label


def _single_combination(view: rules.DatasetView) -> rules.CombinationPlan:
    plan = rules.compute_dataset_plan(view, load_tree())
    assert len(plan.combinations) == 1, [c.label for c in plan.combinations]
    return plan.combinations[0]


def test_dataset_name_contribution_does_not_override_weight() -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "CPI_CONTRIB",
        "Consumer Price Index (CPI) annual and monthly change rates, contribution to annual change",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(rules.CodeView("W", "Weights of expenditure groups (%)"),),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type == "agirlik"
    assert combo.attributes["type_rule_tier"] == "B"


def test_degyear_dim_index_beats_table_title_indicator() -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "CPI_INDEX",
        "Consumer Price Index",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(
                    rules.CodeView(
                        "W", "Weights for Main Groups and Subclasses of Consumer Price Index"
                    ),
                ),
            ),
            rules.DimensionView(
                code="DEGISIM",
                label="Değişim",
                role="other",
                position=1,
                codes=(rules.CodeView("IDX", "Index"),),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type == "endeks"
    assert combo.attributes["type_rule_tier"] == "A"


def test_dataset_name_per_capita_does_not_override_rate() -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "WASTE_RATES",
        "Municipal Waste Statistics, waste per capita and collection rates",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(
                    rules.CodeView(
                        "R",
                        "Rate of Municipal Population Served by Waste Services "
                        "in Total Municipal Population",
                    ),
                ),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type == "oran_pay"
    assert combo.attributes["type_rule_tier"] == "C"


def test_dataset_name_contribution_alone_does_not_fire() -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "FIN_STATS",
        "Financial Intermediary Institution Statistics, contribution to basic indicators",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(rules.CodeView("E", "Employment and Basic Indicators"),),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type != "katki_puan"


def test_tcmb_likidite_rates_name_is_oran_pay_via_tier_c() -> None:
    view = rules.DatasetView(
        "tcmb",
        "TCMB",
        "bie_likidite",
        "Likidite Oranları - Likidite (Asit Test) Oranı",
        dimensions=(
            rules.DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=(
                    rules.CodeView("61", "61 - Telekomünikasyon"),
                    rules.CodeView("62", "62 - Bilgi ve İletişim"),
                ),
            ),
        ),
    )
    combos = rules.compute_dataset_plan(view, load_tree()).combinations
    assert len(combos) == 2
    assert all(combo.measure_type == "oran_pay" for combo in combos)
    assert all(combo.attributes["type_rule_tier"] == "C" for combo in combos)


def _tcmb_serie_combination(
    name: str, category: str, codes: list[rules.CodeView]
) -> rules.CombinationPlan:
    view = rules.DatasetView(
        "tcmb",
        "TCMB",
        "bie_test",
        name,
        source_category=category,
        dimensions=(
            rules.DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=tuple(codes),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree())
    assert len(plan.combinations) == 1
    return plan.combinations[0]


def test_dibs_archive_value_labels_are_fiyat_kur() -> None:
    labels = [
        "TRD211118K17 (2016-11-23/2018-11-21) Deger (24D2K4211118) (Arşiv)",
        "TRH140700A10 (16-07-1999/14-07-2000) Deger (12H1A) (Arşiv)",
        "TRD180821T13 ( 21.08.2019 18.08.2021 ) Değer (24D2) (Arşiv)",
        "TRD230920T16 ( 26.09.2018 23.09.2020 ) Diğer (24DA2) (Arşiv)",
        "TRT040729T22 ( 09.07.2025 04.07.2029 ) Değer (49T2D)",
    ]
    view = rules.DatasetView(
        "tcmb",
        "TCMB",
        "bie_pydibsarsiv",
        "Devlet İç Borçlanma Senetleri",
        dimensions=(
            rules.DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=tuple(
                    rules.CodeView(f"S{index}", label) for index, label in enumerate(labels)
                ),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree())
    assert len(plan.combinations) == 5
    assert all(combo.measure_type == "fiyat_kur" for combo in plan.combinations), [
        combo.measure_type for combo in plan.combinations
    ]
    assert all(
        combo.attributes.get("type_rule") == "dibs_benchmark_value" for combo in plan.combinations
    )


def test_dibs_benchmark_value_currency_is_not_applicable() -> None:
    view = rules.DatasetView(
        "tcmb",
        "TCMB",
        "bie_pydibsarsiv",
        "Devlet İç Borçlanma Senetleri",
        dimensions=(
            rules.DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=(
                    rules.CodeView(
                        "TRT040729T22", "TRT040729T22 ( 09.07.2025 04.07.2029 ) Değer (49T2D)"
                    ),
                    rules.CodeView(
                        "TRD211118K17",
                        "TRD211118K17 (2016-11-23/2018-11-21) Deger (24D2K4211118) (Arşiv)",
                    ),
                ),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree())
    assert len(plan.combinations) == 2
    for combo in plan.combinations:
        assert combo.measure_type == "fiyat_kur"
        assert combo.para_birimi is None
        assert combo.attributes["currency"] == "n/a"
        assert combo.attributes["currency_method"] == "rule"
        assert combo.attributes["nominal"] == "n/a"


@pytest.mark.parametrize(
    "label,expected",
    [
        ("(USD) ABD Doları (Döviz Alış)", "TRY"),
        ("(NOK) Norveç Kronu (Döviz Satış) (Arşiv)", "TRY"),
        ("(GBP) İngiliz Sterlini (Çapraz) (GBP/USD)", "USD"),
        ("(KWD) Kuveyt Dinarı (Çapraz) (KWD/USD)", "USD"),
        ("(LUF) Lüksemburg Frangı (Çapraz) (USD/LUF)", "diger"),
        ("(BDT) Bangladeş Takası (Çapraz) (Arşiv)", "USD"),
        ("100 İtalyan Lireti", "TRY"),
        ("100 İran Riyali", "TRY"),
    ],
)
def test_exchange_rate_currency_is_the_quote_currency(label: str, expected: str) -> None:
    combo = _tcmb_serie_combination(
        "Döviz Kurları",
        "Kurlar",
        [rules.CodeView("S", label, {"unit": "Türk lirası"})],
    )
    assert combo.measure_type == "fiyat_kur"
    assert combo.para_birimi == expected
    assert combo.attributes["currency"] == expected
    assert combo.attributes["currency_method"] == "rule"
    assert combo.attributes["currency_confidence"] == 1.0


def test_dibs_rate_labels_are_oran_pay() -> None:
    for label in (
        "TRD211118K17 (2016-11-23/2018-11-21) Kupon Faiz Oranı (24D2) (Arşiv)",
        "TRD211118K17 (2016-11-23/2018-11-21) Kira Getirisi Oranı (24D2) (Arşiv)",
    ):
        combo = _tcmb_serie_combination(
            "Devlet İç Borçlanma Senetleri",
            "DEVLET İÇ BORÇLANMA SENETLERİ",
            [rules.CodeView("S", label)],
        )
        assert combo.measure_type == "oran_pay", label


def test_tcmb_survey_expected_answer_option_is_cevap_dagilimi() -> None:
    combo = _tcmb_serie_combination(
        "Piyasa Katılımcıları Anketi",
        "BEKLENTİ VE EĞİLİM ANKETİ",
        [
            rules.CodeView(
                "S1",
                "5. (ARTACAK) Gelecek üç aydaki üretim hacmi beklentiniz",
                {"unit": "Yüzde"},
            )
        ],
    )
    assert combo.measure_type == "cevap_dagilimi"
    assert combo.data_nature == "beklenti"


def test_tcmb_survey_realised_answer_option_is_cevap_dagilimi() -> None:
    combo = _tcmb_serie_combination(
        "Piyasa Katılımcıları Anketi",
        "BEKLENTİ VE EĞİLİM ANKETİ",
        [
            rules.CodeView(
                "S2",
                "20. (ARTTI) Son üç ayda alınan iç piyasa siparişlerinizin miktarı",
                {"unit": "Yüzde"},
            )
        ],
    )
    assert combo.measure_type == "cevap_dagilimi"
    assert combo.data_nature == "gerceklesen"


def test_tcmb_ordinary_yuzde_series_is_oran_pay() -> None:
    combo = _tcmb_serie_combination(
        "Kapasite Kullanım Oranı",
        "KAPASİTE KULLANIM",
        [rules.CodeView("S3", "Kapasite Kullanım Oranı", {"unit": "Yüzde"})],
    )
    assert combo.measure_type == "oran_pay"


def test_unit_only_generic_fallback_is_tier_d() -> None:
    # No measure dims: the unit is the only signal, so it must resolve in tier D.
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "U1",
        "Bir Şey",
        attributes={"unit": "Yüzde"},
    )
    combo = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert combo.measure_type == "oran_pay"
    assert combo.attributes["type_rule_tier"] == "D"


def test_evaluate_type_rules_returns_tier() -> None:
    context = rules.TypeContext(
        label_fold="index",
        unit_fold="",
        name_fold="Kasım 2024",
        text="index",
        category_fold="",
        external_code="x",
        is_pka=False,
        is_dibs=False,
        tier_a="index",
        tier_b="",
    )
    assert rules.evaluate_type_rules(context) == ("endeks", "index", "A")
    # The wrapper still returns a 2-tuple for callers/tests.
    assert rules.measure_type_rule(context) == ("endeks", "index")


def test_numbered_answer_options_regex() -> None:
    for label in (
        "24. (1-3 AY) Gelecek üç aylık dönemde",
        "18.6. (Diğer Finansman Kaynakları)_ Gelecek",
        "1. (ARTTI) Son üç ayda",
    ):
        assert rules._ANSWER_OPTION_RE.match(rules.fold(label)), label
    for label in (
        "(TN) TCMB Kotasyonları SATIŞ (%) ...",
        "(2A) TCMB İşlemleri ALIŞ Tutarı (Bin TL) ...",
    ):
        assert not rules._ANSWER_OPTION_RE.match(rules.fold(label)), label


def test_labelled_answer_options_are_cevap_dagilimi() -> None:
    for label in (
        "24. (1-3 AY) Gelecek üç aylık dönemde",
        "18.6. (Diğer Finansman Kaynakları)_ Gelecek",
    ):
        combo = _tcmb_serie_combination(
            "Piyasa Katılımcıları Anketi",
            "BEKLENTİ VE EĞİLİM ANKETİ",
            [rules.CodeView("S", label, {"unit": "Yüzde"})],
        )
        assert combo.measure_type == "cevap_dagilimi", label


def test_participant_count_before_percent_rule() -> None:
    combo = _tcmb_serie_combination(
        "Piyasa Katılımcıları Anketi",
        "BEKLENTİ VE EĞİLİM ANKETİ",
        [rules.CodeView("S", "2. Soruya Cevap Veren Firma Sayısı (Arşiv)", {"unit": "Yüzde"})],
    )
    assert combo.measure_type == "katilimci_sayisi"


def test_respondent_workplace_count_is_participant() -> None:
    combo = _tcmb_serie_combination(
        "Piyasa Katılımcıları Anketi",
        "BEKLENTİ VE EĞİLİM ANKETİ",
        [rules.CodeView("S", "6.Soruya yanıt veren işyeri sayısı", {"unit": "Yüzde"})],
    )
    assert combo.measure_type == "katilimci_sayisi"


def test_pka_en_buyuk_is_distribution_stat_before_interest() -> None:
    combo = _tcmb_serie_combination(
        "Piyasa Katılımcıları Anketi",
        "BEKLENTİ VE EĞİLİM ANKETİ",
        [
            rules.CodeView(
                "S",
                "2A.(En Büyük) Cari Ayın Üç Aylık Hazine İhalesi Yıllık Bileşik "
                "Faiz Oranı Beklentisi",
                {"unit": "Yüzde"},
            )
        ],
    )
    assert combo.measure_type == "dagilim_istatistigi"


def test_contribution_beats_annual_change_phrase() -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "IPI",
        "Industrial Production Index",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(
                    rules.CodeView(
                        "C",
                        "Contribution to change over the same month of the previous year",
                    ),
                ),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type == "katki_puan"


@pytest.mark.parametrize(
    "unit",
    [
        "Turkish Lira/Head (TRY/HD)",
        "Turkish Lira/Pure Number (TRY/Pure Number)",
        "TL/m2",
        "TL/kg",
        "TRY/\u2026",
        "$/\u2026",
    ],
)
def test_per_unit_price_is_fiyat_kur(unit: str) -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "PPI",
        "Producer Price Index by economic activity",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(rules.CodeView("P", "Ortalama Fiyat", {"unit": unit}),),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type == "fiyat_kur", unit


def test_net_change_unit_beats_label_orani() -> None:
    combo = _tcmb_serie_combination(
        "Piyasa Katılımcıları Anketi",
        "BEKLENTİ VE EĞİLİM ANKETİ",
        [
            rules.CodeView(
                "S",
                "10.4. (Kredi/Teminat Oranı)_ Geçen üç ayda, konut alım kredisi "
                "taleplerinin gerçekleşme oranı",
                {"unit": "Net yüzde değişim"},
            )
        ],
    )
    assert combo.measure_type == "denge_yayilma"


def test_tier_c_index_suppressed_by_change_labels() -> None:
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "RGDP",
        "Regional gross domestic product (with chain-linked volume, NUTS 2)",
        dimensions=(
            rules.DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(rules.CodeView("PC", "Percentage change | Chain linked volume index"),),
            ),
        ),
    )
    combo = _single_combination(view)
    assert combo.measure_type is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("İşsizlik Oranı (%)", "oran_pay"),
        ("İşgücüne Katılma Oranı (%)", "oran_pay"),
        ("Kaba doğum hızı (binde)", "hiz_binde"),
        ("Kaba ölüm hızı (binde)", "hiz_binde"),
        ("Bin kişi başına otomobil sayısı", "kisi_basina"),
        ("Kişi başına GSYH (TL)", "kisi_basina"),
        ("Kişi başına toplam elektrik tüketimi (kWh)", "kisi_basina"),
        (
            "Proportion of enterprises with internet access by economic activity",
            "oran_pay",
        ),
    ],
)
def test_name_only_dataset_is_judged_as_tier_b(name: str, expected: str) -> None:
    view = rules.DatasetView("tuik", "TÜİK", "X", name)
    plan = rules.compute_dataset_plan(view, load_tree())
    combo = plan.combinations[0]
    assert combo.codes == {}
    assert combo.measure_type == expected, name
    assert combo.attributes["type_rule_tier"] == "B"


def test_name_only_number_of_house_sales_stays_pending() -> None:
    view = rules.DatasetView("tuik", "TÜİK", "X", "Number of House Sales")
    combo = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert combo.measure_type is None


def test_name_only_uses_unit_hint() -> None:
    view = rules.DatasetView(
        "tuik", "TÜİK", "X", "Kaba doğum hızı", attributes={"unit_hint": "binde"}
    )
    combo = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert combo.measure_type == "hiz_binde"


def test_annual_rate_of_change_is_not_percent_share() -> None:
    measure_type, rule = rules.measure_type_rule(_ctx("Annual rate of change (%)"))
    assert measure_type == "yillik_yuzde_degisim"
    assert rule == "percent_change_annual"


def test_reel_efektif_kur_is_index_not_price() -> None:
    measure_type, _ = rules.measure_type_rule(_ctx("Reel Efektif Döviz Kuru"))
    assert measure_type == "endeks"


def test_exchange_rate_is_price() -> None:
    measure_type, _ = rules.measure_type_rule(_ctx("Döviz Alış (USD)"))
    assert measure_type == "fiyat_kur"


def test_survey_answer_option_is_cevap_dagilimi() -> None:
    measure_type, _ = rules.measure_type_rule(_ctx("1. (ARTTI) Son üç ayda ...", "Yüzde"))
    assert measure_type == "cevap_dagilimi"


def test_answer_option_without_question_number_is_not_cevap_dagilimi() -> None:
    assert rules.measure_type_rule(_ctx("(TN) TCMB Kotasyonları SATIŞ (%) ...", "Yüzde"))[0] != (
        "cevap_dagilimi"
    )
    assert rules.measure_type_rule(_ctx("(2A) TCMB İşlemleri ALIŞ Tutarı (Bin TL) ..."))[0] != (
        "cevap_dagilimi"
    )


def test_interest_rate_expectation_is_oran_pay() -> None:
    label = "Cari Yıl Sonu TCMB Politika Faiz Oranı Beklentisi (%)"
    measure_type, _ = rules.measure_type_rule(_ctx(label, category="BEKLENTİ VE EĞİLİM ANKETİ"))
    assert measure_type == "oran_pay"
    nature, _ = rules.data_nature_rule(
        _survey_view(label), _ctx(label, category="BEKLENTİ VE EĞİLİM ANKETİ")
    )
    assert nature == "beklenti"


def test_per_thousand_is_rate() -> None:
    assert rules.measure_type_rule(_ctx("Kaba doğum hızı (‰)"))[0] == "hiz_binde"
    assert rules.measure_type_rule(_ctx("Per 100 000"))[0] == "hiz_binde"


def test_dibs_benchmark_label_is_price() -> None:
    context = _ctx("2 Yıllık Değer", external_code="bie_pydibs")
    assert rules.measure_type_rule(context)[0] == "fiyat_kur"


# --------------------------------------------------------------------------- #
# Data nature
# --------------------------------------------------------------------------- #


def test_data_nature_projection() -> None:
    nature, _ = rules.data_nature_rule(_survey_view("Nüfus Projeksiyonu"), _ctx("Projeksiyon"))
    assert nature == "tahmin"


def test_data_nature_expectation() -> None:
    nature, _ = rules.data_nature_rule(
        _survey_view("Gelecek Üç Aylık Dönemde"), _ctx("Gelecek Üç Aylık Dönemde")
    )
    assert nature == "beklenti"


def test_data_nature_survey_realised() -> None:
    nature, _ = rules.data_nature_rule(
        _survey_view("(ARTTI) Son üç ayda ...", category="BEKLENTİ VE EĞİLİM ANKETİ"),
        _ctx("(ARTTI) Son üç ayda ...", "Yüzde", category="BEKLENTİ VE EĞİLİM ANKETİ"),
    )
    assert nature == "gerceklesen"


def test_data_nature_survey_without_match_is_pending() -> None:
    nature, _ = rules.data_nature_rule(_survey_view("Sipariş Miktarı"), _ctx("Sipariş Miktarı"))
    assert nature is None


def test_data_nature_non_survey_defaults_realised() -> None:
    view = rules.DatasetView("tuik", "TÜİK", "x", "Nüfus")
    nature, rule = rules.data_nature_rule(view, _ctx("Nüfus"))
    assert (nature, rule) == ("gerceklesen", "default_realised")


# --------------------------------------------------------------------------- #
# Aggregation precedence
# --------------------------------------------------------------------------- #

AGGREGATION_TABLE = [
    # type, default, source, effective, conflict
    ("akim_tutar", "toplam", "sum", "toplam", False),
    ("akim_tutar", "toplam", "avg", "ortalama", True),
    ("akim_tutar", "toplam", "last", "toplam", True),
    ("akim_tutar", "toplam", "max", "toplam", True),
    ("akim_tutar", "toplam", "min", "toplam", True),
    ("stok_tutar", "donem_sonu", "sum", "toplam", True),
    ("stok_tutar", "donem_sonu", "avg", "ortalama", True),
    ("stok_tutar", "donem_sonu", "last", "donem_sonu", False),
    ("stok_tutar", "donem_sonu", "max", "donem_sonu", False),
    ("stok_tutar", "donem_sonu", "min", "donem_sonu", False),
    ("endeks", "ortalama", "sum", "toplam", True),
    ("endeks", "ortalama", "avg", "ortalama", False),
    ("endeks", "ortalama", "last", "ortalama", True),
    # No measure type: the source value cannot conflict (only one side known).
    (None, None, "sum", "toplam", False),
    (None, None, "avg", "ortalama", False),
    (None, None, "last", None, False),
    (None, None, None, None, False),
]


@pytest.mark.parametrize("measure_type,default,source,effective,conflict", AGGREGATION_TABLE)
def test_aggregation_precedence(
    measure_type: str | None,
    default: str | None,
    source: str | None,
    effective: str | None,
    conflict: bool,
) -> None:
    tree = load_tree()
    if measure_type is not None:
        assert tree.aggregation_rule(measure_type) == default
    result, got_conflict = rules.effective_aggregation(measure_type, source, tree)
    assert (result, got_conflict) == (effective, conflict)


# --------------------------------------------------------------------------- #
# Folding
# --------------------------------------------------------------------------- #


def test_turkish_folding() -> None:
    assert rules.fold("İstanbul") == "istanbul"
    assert rules.fold("IŞIK") == "isik"
    assert rules.fold("ı") == "i"
    assert rules.fold("AĞAÇ ÖĞÜN") == "agac ogun"


# --------------------------------------------------------------------------- #
# Measure dimension classification
# --------------------------------------------------------------------------- #


def _dim(code: str, role: str = "other", codes: list[rules.CodeView] | None = None):
    return rules.DimensionView(code=code, label=code, role=role, codes=tuple(codes or []))


def test_classify_measure_dimension() -> None:
    assert rules.classify_dimension(_dim("INDICATOR"), "tuik") is True
    assert rules.classify_dimension(_dim("ADNKS_GOSTERGE"), "tuik") is True
    assert rules.classify_dimension(_dim("SEX"), "tuik") is False
    assert rules.classify_dimension(_dim("NACE_REV2"), "tuik") is False
    assert rules.classify_dimension(_dim("SEASONAL_ADJUST"), "tuik") is False
    assert rules.classify_dimension(_dim("REF_AREA", role="geo"), "tuik") is False
    assert rules.classify_dimension(_dim("SOMETHING_ELSE"), "tuik") is None
    assert rules.classify_dimension(_dim("SERIE"), "tcmb") is True


# --------------------------------------------------------------------------- #
# Combination enumeration
# --------------------------------------------------------------------------- #


def test_enumerate_product() -> None:
    dims = [
        _dim("INDICATOR", codes=[rules.CodeView("I1", "Index"), rules.CodeView("I2", "Percent")]),
        _dim("DEGISIM", codes=[rules.CodeView("D1", "A"), rules.CodeView("D2", "B")]),
    ]
    combos, note = rules.enumerate_combinations(dims)
    assert note is None
    assert len(combos) == 4
    assert all(set(combo) == {"INDICATOR", "DEGISIM"} for combo in combos)


def test_combination_label_follows_dimension_position() -> None:
    indicator = rules.DimensionView(
        code="INDICATOR",
        label="Gösterge",
        role="other",
        position=0,
        codes=(rules.CodeView("ANN", "Annual rate of change (%)"),),
    )
    degisim = rules.DimensionView(
        code="DEGISIM",
        label="Değişim",
        role="other",
        position=1,
        codes=(rules.CodeView("D1", "Index"),),
    )
    dimensions = {"INDICATOR": indicator, "DEGISIM": degisim}
    # codes stay sorted (DEGISIM before INDICATOR) but the label follows position.
    codes = {"DEGISIM": "D1", "INDICATOR": "ANN"}
    assert rules.combination_label(codes, dimensions) == "Annual rate of change (%) | Index"


def test_enumerate_empty_is_single_empty_combination() -> None:
    combos, note = rules.enumerate_combinations([])
    assert combos == [{}]
    assert note is None


def test_enumerate_guard_too_many_codes() -> None:
    codes = [rules.CodeView(f"c{i}", f"c{i}") for i in range(256)]
    combos, note = rules.enumerate_combinations([_dim("INDICATOR", codes=codes)])
    assert combos == []
    assert note


def test_enumerate_guard_product() -> None:
    big = [rules.CodeView(f"a{i}", f"a{i}") for i in range(30)]
    other = [rules.CodeView(f"b{i}", f"b{i}") for i in range(30)]
    combos, note = rules.enumerate_combinations(
        [_dim("INDICATOR", codes=big), _dim("DEGISIM", codes=other)]
    )
    assert combos == []
    assert note


def test_lone_serie_dim_enumerates_fully() -> None:
    codes = [rules.CodeView(f"S{i}", f"Seri {i}") for i in range(3000)]
    combos, note = rules.enumerate_combinations([_dim("SERIE", codes=codes)])
    assert note is None
    assert len(combos) == 3000
    assert all(set(combo) == {"SERIE"} for combo in combos)


def test_single_non_serie_dim_still_guarded() -> None:
    codes = [rules.CodeView(f"c{i}", f"c{i}") for i in range(256)]
    combos, note = rules.enumerate_combinations([_dim("INDICATOR", codes=codes)])
    assert combos == []
    assert note


def test_serie_with_second_measure_dim_still_guarded() -> None:
    serie = _dim("SERIE", codes=[rules.CodeView(f"S{i}", f"S{i}") for i in range(3000)])
    other = _dim("DEGISIM", codes=[rules.CodeView("D1", "D1"), rules.CodeView("D2", "D2")])
    combos, note = rules.enumerate_combinations([serie, other])
    assert combos == []
    assert note


# --------------------------------------------------------------------------- #
# Owned-row protection (pure predicate behind the upsert)
# --------------------------------------------------------------------------- #


def test_is_owned_keeps_manual_and_reviewed_rows() -> None:
    from app.catalog.enrich import _combination_key, _is_owned

    assert _is_owned(
        SimpleNamespace(
            attributes={"reviewed": True}, status="pending", type_method=None, nature_method=None
        )
    )
    # accepted / Jev / rule do NOT protect a stale row.
    assert not _is_owned(
        SimpleNamespace(attributes={}, status="accepted", type_method="jev", nature_method="jev")
    )
    assert not _is_owned(
        SimpleNamespace(attributes={}, status="accepted", type_method="rule", nature_method="rule")
    )
    assert _is_owned(
        SimpleNamespace(attributes={}, status="pending", type_method="manual", nature_method=None)
    )
    assert _is_owned(
        SimpleNamespace(
            attributes={"currency_method": "manual"},
            status="pending",
            type_method=None,
            nature_method=None,
        )
    )
    assert _combination_key({"B": "2", "A": "1"}) == _combination_key({"A": "1", "B": "2"})


# --------------------------------------------------------------------------- #
# Currency / nominal / seasonal
# --------------------------------------------------------------------------- #


def test_currency_detection() -> None:
    assert rules.detect_currency("bin TL") == "TRY"
    assert rules.detect_currency("Thousend TRY") == "TRY"
    assert rules.detect_currency("milyon ABD doları") == "USD"
    assert rules.detect_currency("EUR") == "EUR"
    assert rules.detect_currency("SDR") == "XDR"
    assert rules.detect_currency("Pure number") is None


def test_currency_only_for_monetary_types() -> None:
    assert rules.currency_rule("endeks", "bin TL") == (
        None,
        {"currency": "n/a", "currency_method": "rule"},
    )
    value, attrs = rules.currency_rule("akim_tutar", "bin TL")
    assert (value, attrs) == ("TRY", {"currency": "TRY", "currency_method": "rule"})
    value, attrs = rules.currency_rule("akim_tutar", "Tonnes")
    assert value is None and attrs == {"currency_pending": True}


def test_nominal_reel_rules() -> None:
    assert rules.nominal_rule("akim_tutar", "Reel GSYH") == (
        "reel",
        {"nominal": "reel", "nominal_method": "rule"},
    )
    assert rules.nominal_rule("akim_tutar", "Cari fiyatlarla") == (
        "nominal",
        {"nominal": "nominal", "nominal_method": "rule"},
    )
    assert rules.nominal_rule("akim_tutar", "Zincirlenmiş hacim") == (
        "reel",
        {"nominal": "reel", "nominal_method": "rule"},
    )
    assert rules.nominal_rule("endeks", "Reel") == (
        None,
        {"nominal": "n/a", "nominal_method": "rule"},
    )


def test_bare_real_and_volume_are_not_reel() -> None:
    # "real estate" and "trade volume"/"satış hacmi" are amounts, not real terms.
    assert rules.nominal_rule("akim_tutar", "Real estate") == (None, {"nominal_pending": True})
    assert rules.nominal_rule("akim_tutar", "Trade volume") == (None, {"nominal_pending": True})
    assert rules.nominal_rule("akim_tutar", "Satış hacmi") == (None, {"nominal_pending": True})


def test_seasonal_rule() -> None:
    assert rules.seasonal_rule("Mevsim etkisinden arındırılmış") == "arindirilmis"
    assert rules.seasonal_rule("Unadjusted") == "ham"
    assert rules.seasonal_rule("Nothing here") == "ham"


def test_seasonal_negatives_win_over_positives() -> None:
    assert rules.seasonal_rule("Mevsim etkisinden arındırılmamış") == "ham"
    assert rules.seasonal_rule("Not seasonally adjusted") == "ham"
    assert rules.seasonal_rule("Seasonally unadjusted") == "ham"


def test_ham_word_not_matched_inside_hammadde() -> None:
    dim = rules.DimensionView(
        code="SEASONAL_ADJUST",
        label="Mevsim ve takvim etkisi",
        role="other",
        codes=(rules.CodeView("H", "Hammadde"),),
    )
    assert rules.seasonal_from_dimension(dim) == set()


def test_contribution_only_for_contribution_to() -> None:
    assert rules.measure_type_rule(_ctx("Contribution to total change"))[0] == "katki_puan"
    assert rules.measure_type_rule(_ctx("Katkısı"))[0] == "katki_puan"
    assert rules.measure_type_rule(_ctx("Social contributions"))[0] != "katki_puan"
    assert rules.measure_type_rule(_ctx("Katkı payı"))[0] != "katki_puan"


def test_weight_only_with_a_qualifier() -> None:
    assert rules.measure_type_rule(_ctx("Weights of expenditure groups (%)"))[0] == "agirlik"
    assert rules.measure_type_rule(_ctx("Live weight"))[0] != "agirlik"
    assert rules.measure_type_rule(_ctx("Karkas ağırlığı"))[0] != "agirlik"


def test_ratio_and_share_use_word_boundaries() -> None:
    for label in ("Duration", "Operation", "Registration", "Generation", "Corporation"):
        assert rules.measure_type_rule(_ctx(label))[0] != "oran_pay", label
    assert rules.measure_type_rule(_ctx("Shareholders"))[0] != "oran_pay"


# --------------------------------------------------------------------------- #
# Dataset flags
# --------------------------------------------------------------------------- #


def test_arsiv_only_from_source_label() -> None:
    arsiv = rules.DatasetView("tuik", "TÜİK", "x", "Sanayi (Arşiv)")
    assert rules.is_arsiv(arsiv)
    unlisted = rules.DatasetView(
        "tuik", "TÜİK", "y", "Sanayi", attributes={"unlisted_since": "2020"}
    )
    assert not rules.is_arsiv(unlisted)
    category = rules.DatasetView(
        "tuik", "TÜİK", "z", "Sanayi", attributes={"category_path": [{"title": "ARŞİV"}]}
    )
    assert rules.is_arsiv(category)


def test_revizyon_and_compilation_flags() -> None:
    assert rules.is_revizyon_tablosu(
        rules.DatasetView("tuik", "TÜİK", "DF_X", "X Revision History")
    )
    assert rules.is_cok_konulu_derleme(rules.DatasetView("tuik", "TÜİK", "TURCAT_REEL", "t"))
    assert not rules.is_cok_konulu_derleme(rules.DatasetView("tcmb", "TCMB", "bie_bekodtufe", "t"))


def _portal_view(
    *,
    channel: str = "veriportali",
    downloadable: bool = False,
    dimensions: tuple[rules.DimensionView, ...] = (),
) -> rules.DatasetView:
    return rules.DatasetView(
        "tuik",
        "TÜİK",
        "DF_PORTAL",
        "Bir Rapor",
        attributes={
            "channel": channel,
            "veriportali": {"downloadable": downloadable, "id": "DF_PORTAL+V1.0"},
        },
        dimensions=dimensions,
    )


def test_is_veri_yok_truth_table() -> None:
    # veriportali + downloadable False + no non-time dimension -> True
    assert rules.is_veri_yok(_portal_view()) is True
    # A time dimension does not make the dataset buildable.
    time_dim = rules.DimensionView("TIME_PERIOD", "Zaman", "time")
    assert rules.is_veri_yok(_portal_view(dimensions=(time_dim,))) is True
    # downloadable True -> False
    assert rules.is_veri_yok(_portal_view(downloadable=True)) is False
    # has a non-time dimension -> False
    measure = rules.DimensionView("INDICATOR", "Gösterge", "other")
    assert rules.is_veri_yok(_portal_view(dimensions=(measure,))) is False
    # a databrowser2 dataset -> False (even if it has a portal record)
    assert rules.is_veri_yok(_portal_view(channel="databrowser2")) is False
    # no portal record at all -> False
    no_portal = rules.DatasetView(
        "tuik", "TÜİK", "DF_PORTAL", "Bir Rapor", attributes={"channel": "veriportali"}
    )
    assert rules.is_veri_yok(no_portal) is False


# --------------------------------------------------------------------------- #
# Period-series grouping
# --------------------------------------------------------------------------- #


def test_period_series_candidates_group_by_normalised_name() -> None:
    datasets = [
        ("tuik", "A", "Girişim Sayısı (NUTS 3 Level, 2009-2013)"),
        ("tuik", "B", "Girişim Sayısı (NUTS 3 Level, 2014-2018)"),
        ("tuik", "C", "Girişim Sayısı (NUTS 3 Level, 2019-2023)"),
    ]
    candidates = rules.period_series_candidates(datasets)
    assert len(set(candidates.values())) == 1
    assert set(candidates) == {("tuik", "A"), ("tuik", "B"), ("tuik", "C")}


def test_period_series_unrelated_names_do_not_group() -> None:
    datasets = [
        ("tuik", "A", "Ölüm İstatistikleri 2001-2008"),
        ("tuik", "B", "Tamamen Başka Bir Tablo"),
    ]
    assert rules.period_series_candidates(datasets) == {}


def test_period_series_base_year_groups() -> None:
    datasets = [
        ("tcmb", "KFE_2010", "Konut Fiyat Endeksi (2010=100)"),
        ("tcmb", "KFE_2017", "Konut Fiyat Endeksi (2017=100)"),
    ]
    candidates = rules.period_series_candidates(datasets)
    assert set(candidates) == {("tcmb", "KFE_2010"), ("tcmb", "KFE_2017")}


def test_period_series_does_not_cross_institutions() -> None:
    datasets = [
        ("tuik", "A", "Nüfus (2014-2018)"),
        ("tcmb", "B", "Nüfus (2014-2018)"),
    ]
    assert rules.period_series_candidates(datasets) == {}


# --------------------------------------------------------------------------- #
# Description template
# --------------------------------------------------------------------------- #


def test_turkish_frequency_translation() -> None:
    assert rules.turkish_frequency("monthly") == "aylık"
    assert rules.turkish_frequency("Aylık") == "aylık"
    assert rules.turkish_frequency("mixed") is None


def test_dataset_description_omits_missing_parts() -> None:
    view = rules.DatasetView("tuik", "TÜİK", "x", "Nüfus")
    assert rules.dataset_description("TÜİK", view) == "TÜİK"
    with_parts = rules.dataset_description("TÜİK", view, frequencies=["aylık"], units=["Kişi"])
    assert with_parts == "TÜİK · aylık · Kişi"


def test_description_move_happens_once() -> None:
    tree = load_tree()
    view = rules.DatasetView(
        "tuik", "TÜİK", "x", "Nüfus", description="Kaynak açıklama", attributes={}
    )
    plan = rules.compute_dataset_plan(view, tree)
    assert plan.source_description == "Kaynak açıklama"

    moved = rules.DatasetView(
        "tuik",
        "TÜİK",
        "x",
        "Nüfus",
        description="Şablon açıklama",
        attributes={"source_description": "Kaynak açıklama"},
    )
    plan = rules.compute_dataset_plan(moved, tree)
    assert plan.source_description is None


def test_description_null_stays_null_across_runs() -> None:
    tree = load_tree()
    first = rules.DatasetView("tcmb", "TCMB", "x", "Faiz", description=None, attributes={})
    assert rules.compute_dataset_plan(first, tree).source_description is None
    # The first run wrote the key back as null; our template must not move into it.
    second = rules.DatasetView(
        "tcmb",
        "TCMB",
        "x",
        "Faiz",
        description="TCMB · aylık",
        attributes={"source_description": None},
    )
    assert rules.compute_dataset_plan(second, tree).source_description is None


# --------------------------------------------------------------------------- #
# End-to-end plan
# --------------------------------------------------------------------------- #


def _tuik_dataset() -> rules.DatasetView:
    indicator = rules.DimensionView(
        code="INDICATOR",
        label="Gösterge",
        role="other",
        position=0,
        codes=(
            rules.CodeView("IDX", "Index"),
            rules.CodeView("ANN", "Annual rate of change (%)"),
        ),
    )
    degisim = rules.DimensionView(
        code="DEGISIM",
        label="Değişim",
        role="other",
        position=1,
        codes=(rules.CodeView("D1", "Index"),),
    )
    sex = rules.DimensionView(
        code="SEX",
        label="Cinsiyet",
        role="other",
        position=2,
        codes=(rules.CodeView("T", "Total"),),
    )
    return rules.DatasetView(
        "tuik",
        "Türkiye İstatistik Kurumu",
        "DS1",
        "Sanayi Üretim Endeksi",
        source_category="Sanayi",
        dimensions=(indicator, degisim, sex),
    )


def test_compute_dataset_plan_enumerates_and_flags() -> None:
    tree = load_tree()
    plan = rules.compute_dataset_plan(_tuik_dataset(), tree)
    assert plan.measure_dims["INDICATOR"] is True
    assert plan.measure_dims["DEGISIM"] is True
    assert plan.measure_dims["SEX"] is False
    assert len(plan.combinations) == 2
    assert all(combo.codes.keys() == {"INDICATOR", "DEGISIM"} for combo in plan.combinations)
    assert plan.mevsim_arindirilmis == ("ham",)
    assert plan.revizyon_tablosu is False
    assert plan.veri_yok is False


def test_compute_dataset_plan_single_empty_combination_for_no_measure() -> None:
    tree = load_tree()
    view = rules.DatasetView(
        "tuik",
        "TÜİK",
        "D0",
        "Bir Şey",
        dimensions=(rules.DimensionView("FREQ", "Sıklık", "frequency", codes=()),),
    )
    plan = rules.compute_dataset_plan(view, tree)
    assert len(plan.combinations) == 1
    assert plan.combinations[0].codes == {}
    assert plan.combinations[0].label == "Bir Şey"


def test_tcmb_combination_uses_code_aggregation() -> None:
    tree = load_tree()
    serie = rules.DimensionView(
        "SERIE",
        "Seri",
        "other",
        codes=(
            rules.CodeView(
                "TP.FAIZ",
                "Politika Faizi",
                {"aggregation": "last", "unit": "Yüzde", "frequency": "monthly"},
            ),
        ),
    )
    view = rules.DatasetView("tcmb", "TCMB", "bie_faiz", "Politika Faizi", dimensions=(serie,))
    plan = rules.compute_dataset_plan(view, tree)
    combo = plan.combinations[0]
    assert combo.codes == {"SERIE": "TP.FAIZ"}
    assert combo.source_aggregation == "last"
    assert combo.measure_type == "oran_pay"  # "faiz" in the label
    assert combo.aggregation == "ortalama"
    assert combo.aggregation_conflict is True
