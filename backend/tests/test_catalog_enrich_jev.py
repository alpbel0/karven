"""Unit tests for the Jev enrichment pass (Task 2.2b; no database, no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog import enrich
from app.catalog import enrich_jev as jev
from app.catalog import enrich_rules as rules
from app.catalog.enrich_rules import CodeView, DatasetView, DimensionView
from app.catalog.tree import load_tree
from app.data.models import Dataset, MeasureCombination
from app.prompts.ref import PromptRef

# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


def test_dim_decision_thresholds() -> None:
    assert jev.dim_decision(0.95) is True
    assert jev.dim_decision(0.60) is True
    assert jev.dim_decision(0.59) is None
    assert jev.dim_decision(0.41) is None
    assert jev.dim_decision(0.40) is False
    assert jev.dim_decision(0.05) is False


def test_nominal_value_thresholds() -> None:
    assert jev.nominal_value(0.60) == "reel"
    assert jev.nominal_value(0.59) is None
    assert jev.nominal_value(0.41) is None
    assert jev.nominal_value(0.40) == "nominal"


def test_period_decision_thresholds() -> None:
    assert jev.period_decision(0.60) == "yes"
    assert jev.period_decision(0.59) is None
    assert jev.period_decision(0.40) == "no"


# --------------------------------------------------------------------------- #
# Question building
# --------------------------------------------------------------------------- #


def test_type_and_nature_criteria_are_tree_ids() -> None:
    tree = load_tree()
    type_q = jev.type_question("instructions", tree)
    assert type_q["type"] == "choice"
    assert type_q["instructions"] == "instructions"
    assert set(type_q["criteria"]) == set(tree.measure_types)
    assert all(value for value in type_q["criteria"].values())

    nature_q = jev.nature_question("n", tree)
    assert nature_q["type"] == "choice"
    assert set(nature_q["criteria"]) == set(tree.data_natures)


def test_only_missing_questions_are_asked() -> None:
    tree = load_tree()
    bodies = {"measure_type": "m", "data_nature": "n"}
    row = MeasureCombination(label="L", codes={})
    both = jev.questions_for_combination(row, tree, bodies)
    assert set(both) == {"tur", "nitelik"}

    row.measure_type = "endeks"
    only_nature = jev.questions_for_combination(row, tree, bodies)
    assert set(only_nature) == {"nitelik"}

    row.data_nature = "gerceklesen"
    assert jev.questions_for_combination(row, tree, bodies) == {}


def test_currency_and_nominal_questions_combine() -> None:
    bodies = {"currency": "c", "nominal": "n"}
    row = MeasureCombination(label="L", codes={})
    row.attributes = {"currency_pending": True, "nominal_pending": True}
    questions = jev.questions_for_currency(row, bodies)
    assert set(questions) == {"para", "nominal"}
    assert set(questions["para"]["criteria"]) == set(jev.CURRENCY_IDS)

    row.attributes = {"nominal_pending": True}
    assert set(jev.questions_for_currency(row, bodies)) == {"nominal"}


def test_choice_result_rejects_unknown_option() -> None:
    from app.llm.errors import LLMOutputError

    with pytest.raises(LLMOutputError):
        jev._choice_result({"choice": "nope", "confidence": 0.9}, {"a", "b"})


# --------------------------------------------------------------------------- #
# State building
# --------------------------------------------------------------------------- #


def _dataset(**kwargs) -> Dataset:
    dataset = Dataset(institution_id=1, external_code="X", name="Nüfus")
    for key, value in kwargs.items():
        setattr(dataset, key, value)
    return dataset


def test_category_text_prefers_path_for_tcmb() -> None:
    dataset = _dataset(
        source_category="Kaynak",
        attributes={"category_path": [{"title": "Fiyatlar"}, {"title": "TÜFE"}]},
    )
    assert jev.category_text(dataset, "tcmb") == "Fiyatlar > TÜFE"
    assert jev.category_text(dataset, "hmb") == "Fiyatlar > TÜFE"
    assert jev.category_text(dataset, "tuik") == "Kaynak"


def test_build_dim_state_joins_first_fifteen_labels() -> None:
    dimension = SimpleNamespace(
        code="SEX",
        label="Cinsiyet",
        codes=[SimpleNamespace(label=f"Kod {index}") for index in range(20)],
    )
    state = jev.build_dim_state(_dataset(), "tuik", dimension)
    assert state["kirilim_kodu"] == "SEX"
    assert state["kirilim_adi"] == "Cinsiyet"
    assert state["kod_sayisi"] == 20
    assert state["ornek_kodlar"].count(";") == 14


def test_build_type_state_truncates_and_omits_empty() -> None:
    view = DatasetView(
        "tcmb",
        "TCMB",
        "X",
        "TÜFE",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                codes=(CodeView("S1", "TÜFE", {"unit": "Endeks"}),),
            ),
        ),
    )
    combination = MeasureCombination(label="TÜFE | Endeks", codes={"SERIE": "S1"})
    dataset = _dataset(
        source_category="Fiyatlar",
        attributes={
            "category_path": [{"title": "Fiyatlar"}],
            "source_description": "D" * 500,
        },
    )
    state = jev.build_type_state(dataset, "tcmb", view, combination)
    assert state["kategori"] == "Fiyatlar"
    assert state["gosterge"] == "TÜFE | Endeks"
    assert state["birim"] == "Endeks"
    assert len(state["kaynak_aciklamasi"]) == 300

    dataset.attributes = {}
    without = jev.build_type_state(dataset, "tcmb", view, combination)
    assert "kaynak_aciklamasi" not in without


def test_build_period_state_lists_members() -> None:
    first = _dataset(name="Nüfus 2018")
    first.external_code = "A"
    first.coverage_start = None
    first.coverage_end = None
    second = _dataset(name="Nüfus 2023")
    second.external_code = "B"
    state = jev.build_period_state([first, second], "tuik::nufus")
    assert state["grup"] == "tuik::nufus"
    assert state["uyeler"][0] == "Nüfus 2018 [A] (?–?)"


# --------------------------------------------------------------------------- #
# Rules-after-Jev recomputation
# --------------------------------------------------------------------------- #


def _tuik_view() -> DatasetView:
    return DatasetView(
        "tuik",
        "TÜİK",
        "X",
        "Satışlar",
        dimensions=(
            DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                codes=(CodeView("S", "Satış Tutarı", {"unit": "Bin TL"}),),
            ),
        ),
    )


def _view_with_aggregation(aggregation: str) -> DatasetView:
    return DatasetView(
        "tcmb",
        "TCMB",
        "x",
        "X",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                codes=(CodeView("C", "Etiket", {"aggregation": aggregation}),),
            ),
        ),
    )


def _tcmb_view() -> DatasetView:
    return DatasetView(
        "tcmb",
        "TCMB",
        "bie_faiz",
        "Politika Faizi",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                codes=(
                    CodeView(
                        "TP.FAIZ",
                        "Politika Faizi",
                        {"aggregation": "avg", "unit": "Yüzde"},
                    ),
                ),
            ),
        ),
    )


def test_jev_akim_tutar_recomputes_aggregation_toplam() -> None:
    tree = load_tree()
    row = MeasureCombination(label="Satış Tutarı", codes={"INDICATOR": "S"})
    enrich.apply_jev_types(
        _tuik_view(),
        tree,
        row,
        measure_type="akim_tutar",
        measure_confidence=0.9,
        data_nature="gerceklesen",
        nature_confidence=0.95,
        raw={"tur": {"choice": "akim_tutar", "confidence": 0.9}},
    )
    assert row.aggregation == "toplam"
    assert row.aggregation_conflict is False
    assert row.type_method == "jev"
    assert row.attributes["jev"]["tur"]["choice"] == "akim_tutar"


def test_tcmb_source_avg_with_jev_akim_tutar_conflicts() -> None:
    tree = load_tree()
    row = MeasureCombination(label="Politika Faizi", codes={"SERIE": "TP.FAIZ"})
    enrich.apply_jev_types(
        _tcmb_view(),
        tree,
        row,
        measure_type="akim_tutar",
        measure_confidence=0.9,
        data_nature="gerceklesen",
        nature_confidence=0.9,
        raw={},
    )
    assert row.aggregation == "ortalama"
    assert row.aggregation_conflict is True
    assert row.source_aggregation == "avg"


def test_source_last_without_type_has_no_conflict() -> None:
    tree = load_tree()
    assert rules.effective_aggregation(None, "last", tree) == (None, False)
    assert rules.effective_aggregation(None, "max", tree) == (None, False)
    assert rules.effective_aggregation(None, "sum", tree) == ("toplam", False)


def test_jev_type_recomputes_conflict_for_source_last() -> None:
    tree = load_tree()
    view = _view_with_aggregation("last")
    row = MeasureCombination(label="Faiz", codes={"SERIE": "C"})
    # Rule pass with an unknown type would leave (None, False); Jev then decides.
    assert rules.effective_aggregation(None, "last", tree) == (None, False)
    enrich.apply_jev_types(
        view,
        tree,
        row,
        measure_type="akim_tutar",
        measure_confidence=0.9,
        data_nature="gerceklesen",
        nature_confidence=0.9,
        raw={},
    )
    assert row.source_aggregation == "last"
    assert row.aggregation == "toplam"
    assert row.aggregation_conflict is True


def test_source_avg_with_oran_pay_has_no_conflict() -> None:
    tree = load_tree()
    view = _view_with_aggregation("avg")
    row = MeasureCombination(label="Faiz", codes={"SERIE": "C"})
    enrich.apply_jev_types(
        view,
        tree,
        row,
        measure_type="oran_pay",
        measure_confidence=0.9,
        data_nature="gerceklesen",
        nature_confidence=0.9,
        raw={},
    )
    assert row.aggregation == "ortalama"
    assert row.aggregation_conflict is False


def test_stale_measure_enrich_note_is_removed_on_reenumeration() -> None:
    tree = load_tree()
    view = DatasetView(
        "tcmb",
        "TCMB",
        "x",
        "X",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=(
                    CodeView("A", "A", {"aggregation": "last"}),
                    CodeView("B", "B", {"aggregation": "last"}),
                ),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, tree)
    assert "measure_enrich_note" not in plan.attributes
    dataset = SimpleNamespace(
        id=1,
        attributes={"measure_enrich_note": "measure dim exceeds code limit"},
        dimensions=[],
        description=None,
    )
    session = _FakeSession([])
    enrich.apply_dataset_plan(session, dataset, plan, period_candidate=None)
    assert "measure_enrich_note" not in dataset.attributes


def test_threshold_accepts_060_reviews_at_059() -> None:
    tree = load_tree()
    accepted = MeasureCombination(label="Satış", codes={"INDICATOR": "S"})
    enrich.apply_jev_types(
        _tuik_view(),
        tree,
        accepted,
        measure_type="endeks",
        measure_confidence=0.60,
        data_nature="gerceklesen",
        nature_confidence=1.0,
        raw={},
    )
    assert accepted.status == "accepted"

    review = MeasureCombination(label="Satış", codes={"INDICATOR": "S"})
    enrich.apply_jev_types(
        _tuik_view(),
        tree,
        review,
        measure_type="endeks",
        measure_confidence=0.59,
        data_nature="gerceklesen",
        nature_confidence=1.0,
        raw={},
    )
    assert review.status == "review"


# --------------------------------------------------------------------------- #
# Never overwriting manual / reviewed rows
# --------------------------------------------------------------------------- #


def test_merge_keeps_manual_type() -> None:
    tree = load_tree()
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "endeks"
    row.type_method = "manual"
    plan = rules.derived_combination(
        _tuik_view(),
        tree,
        {"INDICATOR": "S"},
        "L",
        [],
        measure_type="akim_tutar",
        data_nature="gerceklesen",
        type_method="rule",
        nature_method="rule",
        type_confidence=1.0,
        nature_confidence=1.0,
    )
    enrich._merge_plan_into_row(row, plan)
    assert row.measure_type == "endeks"
    assert row.type_method == "manual"


def test_merge_keeps_reviewed_rows() -> None:
    tree = load_tree()
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "endeks"
    row.type_method = "rule"
    row.attributes = {"reviewed": True}
    plan = rules.derived_combination(
        _tuik_view(),
        tree,
        {"INDICATOR": "S"},
        "L",
        [],
        measure_type="akim_tutar",
        data_nature="gerceklesen",
        type_method="rule",
        nature_method="rule",
        type_confidence=1.0,
        nature_confidence=1.0,
    )
    enrich._merge_plan_into_row(row, plan)
    assert row.measure_type == "endeks"


def test_deciding_rule_overwrites_jev_currency() -> None:
    row = MeasureCombination(label="L", codes={}, attributes={})
    enrich.apply_jev_currency(row, currency="USD", confidence=0.95, raw={})
    assert row.para_birimi == "USD"
    plan = SimpleNamespace(
        para_birimi="TRY",
        nominal_mi=None,
        attributes={"currency": "TRY", "currency_method": "rule"},
    )
    attributes = dict(row.attributes or {})
    enrich._merge_currency(row, plan, attributes)
    row.attributes = attributes
    assert row.para_birimi == "TRY"
    assert row.attributes["currency_method"] == "rule"


# --------------------------------------------------------------------------- #
# Stale rule values are reset; Jev values survive
# --------------------------------------------------------------------------- #


def _empty_plan(**overrides) -> rules.CombinationPlan:
    base = dict(
        codes={"INDICATOR": "S"},
        label="L",
        measure_type=None,
        data_nature=None,
        aggregation=None,
        source_aggregation=None,
        aggregation_conflict=False,
        para_birimi=None,
        nominal_mi=None,
        mevsim_arindirilmis="ham",
        kumulatif=False,
        type_method=None,
        nature_method=None,
        type_confidence=None,
        nature_confidence=None,
        attributes={},
    )
    base.update(overrides)
    return rules.CombinationPlan(**base)


def test_rule_owned_type_resets_to_null_when_rule_stops_deciding() -> None:
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "katki_puan"
    row.type_method = "rule"
    row.type_confidence = 1.0
    row.attributes = {"type_rule": "contribution", "type_rule_tier": "B"}
    enrich._merge_plan_into_row(row, _empty_plan())
    assert row.measure_type is None
    assert row.type_method is None
    assert row.type_confidence is None
    assert "type_rule" not in row.attributes
    assert "type_rule_tier" not in row.attributes


def test_rule_owned_nature_and_currency_reset_when_undecided() -> None:
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.data_nature = "gerceklesen"
    row.nature_method = "rule"
    row.nature_confidence = 1.0
    row.attributes = {
        "nature_rule": "default_realised",
        "currency": "TRY",
        "currency_method": "rule",
        "currency_confidence": 1.0,
    }
    plan = _empty_plan(
        measure_type="akim_tutar",
        aggregation="toplam",
        attributes={"currency_pending": True},
    )
    enrich._merge_plan_into_row(row, plan)
    assert row.data_nature is None
    assert row.nature_method is None
    assert "nature_rule" not in row.attributes
    assert row.para_birimi is None
    assert "currency_method" not in row.attributes
    assert row.attributes.get("currency_pending") is True


def test_jev_type_is_never_reset_by_rules() -> None:
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "katki_puan"
    row.type_method = "jev"
    row.type_confidence = 0.9
    row.attributes = {"type_rule_tier": "B"}
    enrich._merge_plan_into_row(row, _empty_plan())
    assert row.measure_type == "katki_puan"
    assert row.type_method == "jev"
    assert row.attributes.get("type_rule_tier") == "B"


def test_deciding_rule_overwrites_jev_type_and_recomputes_status() -> None:
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "stok_tutar"
    row.type_method = "jev"
    row.type_confidence = 0.39
    row.attributes = {"jev": {"tur": {"choice": "stok_tutar", "confidence": 0.39}}}
    plan = _empty_plan(
        measure_type="fiyat_kur",
        data_nature="gerceklesen",
        type_method="rule",
        nature_method="rule",
        type_confidence=1.0,
        nature_confidence=1.0,
        aggregation="ortalama",
        para_birimi="TRY",
        attributes={
            "type_rule": "exchange_rate",
            "type_rule_tier": "B",
            "currency": "TRY",
            "currency_method": "rule",
        },
    )
    enrich._merge_plan_into_row(row, plan)
    assert row.measure_type == "fiyat_kur"
    assert row.type_method == "rule"
    assert row.type_confidence == 1.0
    # The old Jev answer stays for audit.
    assert row.attributes["jev"]["tur"]["choice"] == "stok_tutar"
    assert row.status == "accepted"


def test_rule_dibs_benchmark_overwrites_jev_currency_and_accepts() -> None:
    view = DatasetView(
        "tcmb",
        "TCMB",
        "bie_pydibsarsiv",
        "Devlet İç Borçlanma Senetleri",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=(
                    CodeView("TRT1", "TRT040729T22 ( 09.07.2025 04.07.2029 ) Değer (49T2D)"),
                ),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert plan.measure_type == "fiyat_kur"
    assert plan.attributes["currency"] == "n/a"
    row = MeasureCombination(label=plan.label, codes=plan.codes)
    row.measure_type = "fiyat_kur"
    row.type_method = "rule"
    row.type_confidence = 1.0
    row.attributes = {
        "currency": "USD",
        "currency_method": "jev",
        "currency_confidence": 0.9,
        "jev": {"para": {"choice": "USD", "confidence": 0.9}},
    }
    enrich._merge_plan_into_row(row, plan)
    assert row.para_birimi is None
    assert row.attributes["currency"] == "n/a"
    assert row.attributes["currency_method"] == "rule"
    assert row.attributes["jev"]["para"]["choice"] == "USD"  # audit kept
    assert row.status == "accepted"


def test_exchange_rule_overwrites_jev_currency_with_quote() -> None:
    view = DatasetView(
        "tcmb",
        "TCMB",
        "bie_dkefkytl",
        "Efektif Kurlar",
        source_category="Kurlar",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=(CodeView("S", "(USD) ABD Doları (Döviz Alış)"),),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert plan.para_birimi == "TRY"
    row = MeasureCombination(label=plan.label, codes=plan.codes)
    row.measure_type = "fiyat_kur"
    row.type_method = "jev"
    row.type_confidence = 0.9
    row.attributes = {
        "currency": "diger",
        "currency_method": "jev",
        "currency_confidence": 0.9,
        "jev": {"para": {"choice": "diger", "confidence": 0.9}},
    }
    enrich._merge_plan_into_row(row, plan)
    assert row.para_birimi == "TRY"
    assert row.attributes["currency"] == "TRY"
    assert row.attributes["currency_method"] == "rule"
    assert row.attributes["jev"]["para"]["choice"] == "diger"  # audit kept
    assert row.status == "accepted"


def test_upsert_updates_jev_accepted_row_so_rule_wins() -> None:
    view = DatasetView(
        "tcmb",
        "TCMB",
        "bie_pydibsarsiv",
        "Devlet İç Borçlanma Senetleri",
        dimensions=(
            DimensionView(
                code="SERIE",
                label="Seri",
                role="other",
                position=0,
                codes=(CodeView("TRT1", "TRT040729T22 ( 09.07.2025 04.07.2029 ) Değer (49T2D)"),),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    row = MeasureCombination(label=plan.label, codes=plan.codes)
    row.measure_type = "fiyat_kur"
    row.type_method = "jev"
    row.type_confidence = 0.9
    row.status = "accepted"
    row.attributes = {
        "currency": "TRY",
        "currency_method": "jev",
        "currency_confidence": 0.9,
        "jev": {"para": {"choice": "TRY", "confidence": 0.9}},
    }
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    session = _FakeSession([row])
    counts = enrich.upsert_combinations(session, dataset, (plan,))
    assert counts["updated"] == 1
    assert row.para_birimi is None
    assert row.attributes["currency"] == "n/a"
    assert row.attributes["currency_method"] == "rule"
    assert row.status == "accepted"


def test_normalize_currency_maps_unknown_to_diger() -> None:
    assert rules.normalize_currency("GBP") == "diger"
    assert rules.normalize_currency("LUF") == "diger"
    assert rules.normalize_currency("USD") == "USD"
    assert rules.normalize_currency(None) == "diger"


def test_jev_currency_outside_allowed_becomes_diger() -> None:
    row = MeasureCombination(label="L", codes={}, attributes={})
    enrich.apply_jev_currency(
        row, currency="GBP", confidence=0.9, raw={"para": {"choice": "GBP"}}
    )
    assert row.para_birimi == "diger"
    assert row.attributes["currency"] == "diger"


def test_rule_currency_outside_allowed_becomes_diger() -> None:
    row = MeasureCombination(label="L", codes={}, attributes={})
    plan = SimpleNamespace(
        para_birimi="GBP",
        nominal_mi=None,
        attributes={"currency": "GBP", "currency_method": "rule"},
    )
    attributes = dict(row.attributes or {})
    enrich._merge_currency(row, plan, attributes)
    row.attributes = attributes
    assert row.para_birimi == "diger"
    assert row.attributes["currency"] == "diger"


def test_manual_type_is_not_overwritten_by_rule() -> None:
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "endeks"
    row.type_method = "manual"
    row.type_confidence = 1.0
    enrich._merge_plan_into_row(
        row,
        _empty_plan(measure_type="fiyat_kur", type_method="rule", type_confidence=1.0),
    )
    assert row.measure_type == "endeks"
    assert row.type_method == "manual"


# --------------------------------------------------------------------------- #
# Error counting and abort
# --------------------------------------------------------------------------- #


def test_pool_counts_errors_and_aborts_after_consecutive_failures() -> None:
    def boom(item: dict) -> str:
        raise RuntimeError("provider down")

    progress = jev.run_pool(
        [{"combination_id": index} for index in range(10)],
        boom,
        step="types",
        workers=1,
        out=lambda _line: None,
        failure_limit=3,
    )
    assert progress.errors == 3
    assert progress.aborted is True
    assert progress.done == 3


def test_pool_records_success() -> None:
    progress = jev.run_pool(
        [{"combination_id": index} for index in range(5)],
        lambda item: "accepted",
        step="types",
        workers=2,
        out=lambda _line: None,
    )
    assert progress.errors == 0
    assert progress.accepted == 5
    assert progress.aborted is False


# --------------------------------------------------------------------------- #
# Worker pool commits per item (fake session factory)
# --------------------------------------------------------------------------- #


class _RecordingSession:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def __enter__(self) -> _RecordingSession:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def commit(self) -> None:
        self._log.append("commit")


class _RecordingFactory:
    def __init__(self) -> None:
        self.log: list[str] = []

    def __call__(self) -> _RecordingSession:
        return _RecordingSession(self.log)


def test_worker_pool_commits_once_per_successful_item() -> None:
    factory = _RecordingFactory()

    def worker(item: dict) -> str:
        with factory() as session:
            session.commit()
        return "accepted"

    progress = jev.run_pool(
        [{"combination_id": index} for index in range(6)],
        worker,
        step="types",
        workers=3,
        out=lambda _line: None,
    )
    assert progress.errors == 0
    assert factory.log.count("commit") == 6


# --------------------------------------------------------------------------- #
# Prompt seeding
# --------------------------------------------------------------------------- #


def test_seed_missing_skips_active_versions() -> None:
    seeded: list[str] = []
    jev._seed_missing({"catalog.enrich.measure_type"}, lambda key, body: seeded.append(key))
    assert "catalog.enrich.measure_type" not in seeded
    assert set(seeded) == set(jev.PROMPT_KEYS.values()) - {"catalog.enrich.measure_type"}
    assert all(body for body in seeded)


# --------------------------------------------------------------------------- #
# Resumability
# --------------------------------------------------------------------------- #


def test_filling_only_nature_keeps_rule_type_method() -> None:
    tree = load_tree()
    row = MeasureCombination(label="L", codes={"INDICATOR": "S"})
    row.measure_type = "endeks"
    row.type_method = "rule"
    row.type_confidence = 1.0
    enrich.apply_jev_types(
        _tuik_view(),
        tree,
        row,
        measure_type=None,
        measure_confidence=None,
        data_nature="gerceklesen",
        nature_confidence=0.9,
        raw={"nitelik": {"choice": "gerceklesen"}},
    )
    assert row.measure_type == "endeks"
    assert row.type_method == "rule"
    assert row.data_nature == "gerceklesen"
    assert row.nature_method == "jev"


def test_decided_rows_ask_nothing() -> None:
    tree = load_tree()
    row = MeasureCombination(label="L", codes={})
    enrich.apply_jev_types(
        _tuik_view(),
        tree,
        row,
        measure_type="endeks",
        measure_confidence=0.9,
        data_nature="gerceklesen",
        nature_confidence=0.9,
        raw={},
    )
    assert jev.questions_for_combination(row, tree, {"measure_type": "m", "data_nature": "n"}) == {}


# --------------------------------------------------------------------------- #
# Persistence merge (Step -1)
# --------------------------------------------------------------------------- #


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, rows: list | None = None) -> None:
        self.rows = rows or []
        self.added: list = []
        self.deleted: list = []
        self.flushed = False

    def scalars(self, statement: object) -> _Rows:
        return _Rows(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def delete(self, obj: object) -> None:
        self.deleted.append(obj)

    def flush(self) -> None:
        self.flushed = True


def test_upsert_does_not_replace_jev_value_with_rule_null() -> None:
    row = MeasureCombination(label="L", codes={"A": "1"})
    row.measure_type = "akim_tutar"
    row.type_method = "jev"
    layout = rules.CombinationPlan(
        codes={"A": "1"},
        label="L",
        measure_type=None,
        data_nature=None,
        aggregation=None,
        source_aggregation=None,
        aggregation_conflict=False,
        para_birimi=None,
        nominal_mi=None,
        mevsim_arindirilmis="ham",
        kumulatif=False,
        type_method=None,
        nature_method=None,
        type_confidence=None,
        nature_confidence=None,
        attributes={},
    )
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    session = _FakeSession([row])
    counts = enrich.upsert_combinations(session, dataset, (layout,))
    assert row.measure_type == "akim_tutar"
    assert counts["updated"] == 1


def test_upsert_deletes_stale_unowned_but_keeps_owned() -> None:
    stale = MeasureCombination(label="old", codes={"A": "1"})
    owned = MeasureCombination(label="owned", codes={"A": "2"})
    owned.attributes = {"reviewed": True}
    keep = MeasureCombination(label="keep", codes={"A": "1", "B": "9"})
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    layout = rules.CombinationPlan(
        codes={"A": "1", "B": "9"},
        label="keep",
        measure_type=None,
        data_nature=None,
        aggregation=None,
        source_aggregation=None,
        aggregation_conflict=False,
        para_birimi=None,
        nominal_mi=None,
        mevsim_arindirilmis="ham",
        kumulatif=False,
        type_method=None,
        nature_method=None,
        type_confidence=None,
        nature_confidence=None,
        attributes={},
    )
    session = _FakeSession([stale, owned, keep])
    counts = enrich.upsert_combinations(session, dataset, (layout,))
    assert stale in session.deleted
    assert owned not in session.deleted
    assert counts["deleted"] == 1


def test_rule_pass_uses_jev_effective_type_for_applicability() -> None:
    view = DatasetView(
        "tuik",
        "TÜİK",
        "X",
        "Hammadde Alımları",
        dimensions=(
            DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(CodeView("T", "Alım Tutarı"),),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert plan.measure_type is None  # the rules do not decide this combination
    row = MeasureCombination(label="Alım Tutarı", codes={"INDICATOR": "T"})
    row.measure_type = "akim_tutar"
    row.type_method = "jev"
    row.type_confidence = 0.9
    row.attributes = {"jev": {"tur": {"choice": "akim_tutar", "confidence": 0.9}}}
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    session = _FakeSession([row])
    enrich.upsert_combinations(session, dataset, (plan,), view=view, tree=load_tree())
    assert row.attributes.get("nominal") != "n/a"
    assert row.attributes.get("nominal_pending") is True
    assert row.attributes.get("currency_pending") is True


def test_rule_pass_rule_typed_non_amount_gets_nominal_na() -> None:
    view = DatasetView(
        "tuik",
        "TÜİK",
        "X",
        "İşsizlik",
        dimensions=(
            DimensionView(
                code="INDICATOR",
                label="Gösterge",
                role="other",
                position=0,
                codes=(CodeView("R", "İşsizlik Oranı (%)"),),
            ),
        ),
    )
    plan = rules.compute_dataset_plan(view, load_tree()).combinations[0]
    assert plan.measure_type == "oran_pay"
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    session = _FakeSession([])
    enrich.upsert_combinations(session, dataset, (plan,), view=view, tree=load_tree())
    created = session.added[0]
    assert created.nominal_mi is None
    assert created.attributes["nominal"] == "n/a"
    assert created.attributes["nominal_method"] == "rule"


def test_upsert_deletes_stale_accepted_jev_row() -> None:
    stale = MeasureCombination(label="old", codes={})
    stale.status = "accepted"
    stale.type_method = "jev"
    stale.nature_method = "jev"
    stale.attributes = {"jev": {"tur": {"choice": "endeks"}}}
    keep = MeasureCombination(label="keep", codes={"INDICATOR": "S"})
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    session = _FakeSession([stale, keep])
    counts = enrich.upsert_combinations(session, dataset, (_empty_plan(),))
    assert stale in session.deleted
    assert keep not in session.deleted
    assert counts["deleted"] == 1


def test_upsert_keeps_stale_reviewed_row() -> None:
    reviewed = MeasureCombination(label="old", codes={})
    reviewed.status = "accepted"
    reviewed.attributes = {"reviewed": True}
    dataset = Dataset(institution_id=1, external_code="X", name="X")
    dataset.id = 1
    session = _FakeSession([reviewed])
    counts = enrich.upsert_combinations(session, dataset, (_empty_plan(),))
    assert reviewed not in session.deleted
    assert counts["deleted"] == 0


def test_recompute_dataset_flags_unions_combinations_and_seasonal_dim() -> None:
    first = MeasureCombination(label="a", codes={})
    first.para_birimi = "USD"
    first.nominal_mi = "reel"
    first.mevsim_arindirilmis = "ham"
    second = MeasureCombination(label="b", codes={})
    second.para_birimi = "TRY"
    second.nominal_mi = None
    second.mevsim_arindirilmis = "arindirilmis"
    dataset = SimpleNamespace(
        id=1,
        dimensions=[
            SimpleNamespace(
                code="SEASONAL_ADJUST",
                codes=[SimpleNamespace(label="Takvim etkisinden arındırılmış")],
            )
        ],
    )
    session = _FakeSession([first, second])
    enrich.recompute_dataset_flags(session, dataset)
    assert dataset.para_birimi == ["TRY", "USD"]
    assert dataset.nominal_mi == ["reel"]
    assert dataset.mevsim_arindirilmis == ["arindirilmis", "ham"]


def test_apply_dataset_plan_keeps_period_series_and_writes_null_source_once() -> None:
    tree = load_tree()
    dataset = SimpleNamespace(
        id=1,
        external_code="X",
        name="Nüfus",
        donem_serisi=True,
        donem_serisi_grubu="tuik::nufus",
        description="template",
        attributes={},
        dimensions=[],
    )
    view = rules.DatasetView("tuik", "TÜİK", "X", "Nüfus", description=None, attributes={})
    plan = rules.compute_dataset_plan(view, tree)
    assert plan.source_description is None
    session = _FakeSession([])
    enrich.apply_dataset_plan(session, dataset, plan, period_candidate=None)
    assert dataset.donem_serisi is True
    assert dataset.donem_serisi_grubu == "tuik::nufus"
    assert "source_description" in dataset.attributes
    assert dataset.attributes["source_description"] is None


# --------------------------------------------------------------------------- #
# Performance: one view + one flag recompute per dataset
# --------------------------------------------------------------------------- #


def test_collect_types_builds_one_view_per_dataset(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_build(dataset, code, name):  # noqa: ANN001
        calls["n"] += 1
        return DatasetView("tuik", "TÜİK", "X", "N", dimensions=())

    monkeypatch.setattr(enrich, "_build_view", fake_build)
    dataset = SimpleNamespace(
        id=5, name="Nüfus", source_category=None, attributes={}, dimensions=[]
    )
    combinations = [
        SimpleNamespace(
            id=1,
            label="A",
            codes={},
            measure_type=None,
            data_nature=None,
            type_confidence=None,
            nature_confidence=None,
        ),
        SimpleNamespace(
            id=2,
            label="B",
            codes={},
            measure_type="endeks",
            data_nature=None,
            type_confidence=1.0,
            nature_confidence=None,
        ),
    ]
    rows = [(combination, dataset, "tuik", "TÜİK") for combination in combinations]

    class _Session:
        def execute(self, statement):  # noqa: ANN001
            return _Rows(rows)

    runner = jev.JevPass(
        session_factory=lambda: None, jev_factory=lambda: None, tree=load_tree()
    )
    runner._bodies = {"measure_type": "m", "data_nature": "n"}
    runner._refs = {
        "measure_type": PromptRef("catalog.enrich.measure_type", 1, "x"),
        "data_nature": PromptRef("catalog.enrich.data_nature", 1, "x"),
    }
    items = runner._collect_types(_Session())
    assert calls["n"] == 1  # two combinations, one dataset -> one view
    assert len(items) == 2


def test_recompute_touched_flags_once_per_dataset(monkeypatch) -> None:
    recorded: list[int] = []
    commits: list[int] = []

    monkeypatch.setattr(
        enrich,
        "recompute_dataset_flags",
        lambda session, dataset: recorded.append(dataset.id),
    )

    class _Session:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        def get(self, model, ident):  # noqa: ANN001
            return SimpleNamespace(id=ident) if model is Dataset else None

        def commit(self) -> None:
            commits.append(1)

    errors = jev.recompute_touched_flags(
        lambda: _Session(), [5, 5, 7, 7, 7], step="types", out=lambda _line: None
    )
    assert errors == 0
    assert sorted(recorded) == [5, 7]
    assert len(commits) == 2


# --------------------------------------------------------------------------- #
# refresh-status
# --------------------------------------------------------------------------- #


def test_refresh_statuses_resolves_dim_with_new_threshold() -> None:
    # 0.65 was in the old 0.30..0.70 review band but is accepted at 0.60.
    dimension = SimpleNamespace(
        attributes={"measure_review": True, "measure_score": 0.65},
        is_measure=None,
        measure_source=None,
    )
    combination = MeasureCombination(label="L", codes={})
    combination.measure_type = "endeks"
    combination.type_method = "rule"
    combination.type_confidence = 1.0
    combination.data_nature = "gerceklesen"
    combination.nature_method = "rule"
    combination.nature_confidence = 1.0

    class _Session:
        def __init__(self) -> None:
            self._scalars = 0

        def scalars(self, statement):  # noqa: ANN001
            self._scalars += 1
            if self._scalars == 1:
                return _Rows([dimension])
            if self._scalars == 2:
                return _Rows([combination])
            return _Rows([])

        def flush(self) -> None:
            return None

    counts = jev.refresh_statuses(_Session())
    assert dimension.is_measure is True
    assert dimension.measure_source == "jev"
    assert "measure_review" not in dimension.attributes
    assert counts["dims"] == 1


def test_noul_decisiveness_for_nominal() -> None:
    score, confidence = jev._noul_result({"noul": 0.16, "type": "noul"})
    assert score == pytest.approx(0.16)
    assert confidence == pytest.approx(0.84)
    assert jev.nominal_value(0.16) == "nominal"
    assert jev.nominal_value(0.55) is None


def _decided_amount_row() -> MeasureCombination:
    row = MeasureCombination(label="L", codes={})
    row.measure_type = "akim_tutar"
    row.type_method = "rule"
    row.type_confidence = 1.0
    row.data_nature = "gerceklesen"
    row.nature_method = "rule"
    row.nature_confidence = 1.0
    row.para_birimi = "TRY"
    row.attributes = {
        "currency": "TRY",
        "currency_method": "rule",
        "currency_confidence": 1.0,
    }
    return row


def test_nominal_016_is_accepted() -> None:
    row = _decided_amount_row()
    enrich.apply_jev_nominal(
        row,
        value="nominal",
        score=0.16,
        confidence=0.84,
        raw={"nominal": {"noul": 0.16, "type": "noul"}},
    )
    assert row.nominal_mi == "nominal"
    assert row.attributes["nominal_confidence"] == pytest.approx(0.84)
    assert row.status == "accepted"


def test_nominal_055_is_review() -> None:
    row = _decided_amount_row()
    enrich.apply_jev_nominal(
        row,
        value=None,
        score=0.55,
        confidence=0.55,
        raw={"nominal": {"noul": 0.55, "type": "noul"}},
    )
    assert row.nominal_mi is None
    assert row.status == "review"


def test_refresh_statuses_repairs_old_noul_nominal_confidence() -> None:
    row = _decided_amount_row()
    row.nominal_mi = "nominal"
    row.attributes.update(
        {
            "nominal": "nominal",
            "nominal_method": "jev",
            "nominal_confidence": 0.16,
            "jev": {"nominal": {"noul": 0.16, "type": "noul"}},
        }
    )

    class _Session:
        def __init__(self) -> None:
            self._scalars = 0

        def scalars(self, statement):  # noqa: ANN001
            self._scalars += 1
            return _Rows([row]) if self._scalars == 2 else _Rows([])

        def flush(self) -> None:
            return None

    counts = jev.refresh_statuses(_Session())
    assert row.attributes["nominal_confidence"] == pytest.approx(0.84)
    assert row.status == "accepted"
    assert counts["nominal"] == 1


# --------------------------------------------------------------------------- #
# Manual decisions are never re-asked
# --------------------------------------------------------------------------- #


def test_set_period_marks_reviewed_and_clears_review() -> None:
    first = SimpleNamespace(
        attributes={"period_series_candidate": "g", "period_series_review": True},
        donem_serisi=False,
        donem_serisi_grubu=None,
    )
    second = SimpleNamespace(
        attributes={"period_series_candidate": "g"},
        donem_serisi=False,
        donem_serisi_grubu=None,
    )

    class _Session:
        def scalars(self, statement):  # noqa: ANN001
            return _Rows([first, second])

        def flush(self) -> None:
            return None

    count = jev.set_period(_Session(), "g", accepted=True)
    assert count == 2
    for member in (first, second):
        assert member.attributes.get("period_series_reviewed") is True
        assert member.attributes.get("period_series_method") == "manual"
        assert "period_series_review" not in member.attributes
        assert "period_series_candidate" not in member.attributes
        assert member.donem_serisi is True
        assert member.donem_serisi_grubu == "g"


def test_period_group_decided_predicate() -> None:
    pending = SimpleNamespace(
        attributes={"period_series_review": True},
        donem_serisi=False,
        donem_serisi_grubu=None,
    )
    assert not jev._period_group_decided([pending], "g")

    reviewed = SimpleNamespace(
        attributes={"period_series_reviewed": True},
        donem_serisi=False,
        donem_serisi_grubu=None,
    )
    assert jev._period_group_decided([reviewed], "g")

    manual = SimpleNamespace(
        attributes={"period_series_method": "manual"},
        donem_serisi=False,
        donem_serisi_grubu=None,
    )
    assert jev._period_group_decided([manual], "g")

    accepted = SimpleNamespace(attributes={}, donem_serisi=True, donem_serisi_grubu="g")
    assert jev._period_group_decided([accepted], "g")


def test_collect_dims_skips_manual_dimensions() -> None:
    dataset = SimpleNamespace(
        id=1, name="Nüfus", source_category=None, attributes={}, dimensions=[]
    )
    manual = SimpleNamespace(
        id=1,
        dataset_id=1,
        code="MANUAL",
        label="Manual",
        measure_source="manual",
        codes=[],
        attributes={},
    )
    rule_dim = SimpleNamespace(
        id=2,
        dataset_id=1,
        code="PENDING",
        label="Pending",
        measure_source="rule",
        codes=[],
        attributes={},
    )

    class _Session:
        def execute(self, statement):  # noqa: ANN001
            return _Rows([(manual, dataset, "tuik"), (rule_dim, dataset, "tuik")])

    runner = jev.JevPass(
        session_factory=lambda: None, jev_factory=lambda: None, tree=load_tree()
    )
    runner._bodies = {"measure_dim": "m"}
    items = runner._collect_dims(_Session())
    assert len(items) == 1
    assert items[0]["dimension_id"] == 2
