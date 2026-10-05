"""Unit tests for the catalog series search flow (scripted Jev/EVREN, no DB)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.catalog import search
from app.catalog.tree import load_tree
from app.config import settings as app_settings
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    MeasureCombination,
)
from app.llm.errors import LLMRequestError
from app.prompts.ref import PromptRef

REQUEST = "Türkiye geneli toplam konut satış sayısı, aylık"

TREE = load_tree()
BRANCH = TREE.branch_order[0]
LEAF = TREE.branch_leaves[BRANCH][0]

BODIES = {
    "branch": "Bu isteği yanıtlayacak veri '{ad}' dalında mı? {tanim}{degildir}",
    "leaf": "Bu isteği yanıtlayacak veri '{ad}' konusunda mı? {tanim}{degildir}",
    "dataset": "Veri seti isteği yanıtlıyor mu? {dataset}",
    "dimension": "{istek} için {boyut} seçin.",
    "verify": "'{series}' serisi '{istek}' isteğini karşılıyor mu?",
    "rewrite": "İsteği aynı anlamı koruyarak yeniden yaz: {istek}",
}
REFS = {
    name: PromptRef(key=f"catalog.search.{name}", version=1, checksum="checksum") for name in BODIES
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def make_dataset(
    dataset_id: int,
    code: str,
    name: str,
    *,
    arsiv: bool = False,
    revizyon: bool = False,
    cok: bool = False,
    single_code: bool = False,
) -> Dataset:
    dataset = Dataset(id=dataset_id, institution_id=1, external_code=code, name=name)
    dataset.arsiv = arsiv
    dataset.revizyon_tablosu = revizyon
    dataset.cok_konulu_derleme = cok
    dataset.attributes = {}
    codes = [
        DimensionCode(
            id=1,
            code="POP",
            label="Nüfus",
            attributes={"frequency": "monthly", "unit": "Kişi"},
        )
    ]
    if not single_code:
        codes.append(
            DimensionCode(
                id=2,
                code="SALE",
                label="Satış",
                attributes={"frequency": "monthly", "unit": "Adet"},
            )
        )
    dataset.dimensions = [
        DatasetDimension(code="INDICATOR", label="Gösterge", position=0, role="other", codes=codes)
    ]
    dataset.measure_combinations = [
        MeasureCombination(
            id=dataset_id,
            dataset_id=dataset_id,
            codes={"INDICATOR": "POP"},
            label="Nüfus",
            measure_type="stok",
            data_nature="gerceklesen",
            aggregation="toplam",
            para_birimi="yok",
            nominal_mi="nominal",
            mevsim_arindirilmis="mevsim_arindirilmamis",
            kumulatif=False,
        )
    ]
    return dataset


def make_multi_dim_dataset(
    dataset_id: int, code: str, name: str, *, dimension_count: int = 3
) -> Dataset:
    dataset = Dataset(id=dataset_id, institution_id=1, external_code=code, name=name)
    dataset.attributes = {}
    dimensions = []
    for index in range(dimension_count):
        dimensions.append(
            DatasetDimension(
                code=f"DIM{index}",
                label=f"Boyut{index}",
                position=index,
                role="other",
                codes=[
                    DimensionCode(
                        id=10 * index + 1,
                        code=f"{index}A",
                        label=f"{index} A",
                        attributes={"frequency": "monthly", "unit": "Adet"},
                    ),
                    DimensionCode(id=10 * index + 2, code=f"{index}B", label=f"{index} B"),
                ],
            )
        )
    dataset.dimensions = dimensions
    dataset.measure_combinations = [
        MeasureCombination(
            id=dataset_id,
            dataset_id=dataset_id,
            codes={"DIM0": "0A"},
            label="x",
            measure_type="akim",
            data_nature="gerceklesen",
            aggregation="toplam",
            para_birimi="yok",
            nominal_mi="nominal",
            mevsim_arindirilmis="mevsim_arindirilmamis",
            kumulatif=False,
        )
    ]
    return dataset


class FakeCatalog:
    """Stand-in for the candidate SQL, filtering by the requested archive mode."""

    def __init__(self, rows: list[tuple[Dataset, str, str]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: Any, *, leaf_ids: Any, branch_ids: Any, arsiv: str = "exclude"):
        self.calls.append(
            {
                "leaf_ids": list(leaf_ids),
                "branch_ids": list(branch_ids),
                "arsiv": arsiv,
            }
        )
        if arsiv == "only":
            return [row for row in self.rows if row[0].arsiv]
        return [row for row in self.rows if not row[0].arsiv]


class FakeSession:
    """Session double that fails on every write and answers the Series lookup."""

    def __init__(self, series_id: int | None = None) -> None:
        self.series_id = series_id
        self.scalar_calls = 0
        self.writes = 0

    def scalar(self, statement: Any) -> int | None:
        self.scalar_calls += 1
        return self.series_id

    def add(self, obj: Any) -> None:  # pragma: no cover - must never run
        self.writes += 1
        raise AssertionError("search must not add rows")

    def flush(self) -> None:  # pragma: no cover - must never run
        self.writes += 1
        raise AssertionError("search must not flush")

    def commit(self) -> None:  # pragma: no cover - must never run
        self.writes += 1
        raise AssertionError("search must not commit")

    def delete(self, obj: Any) -> None:  # pragma: no cover - must never run
        self.writes += 1
        raise AssertionError("search must not delete")

    def execute(self, statement: Any) -> Any:  # pragma: no cover
        raise AssertionError("candidate loading must be patched in unit tests")


class FakeJev:
    """Scripted Jev: branch/leaf/dataset scores, dimension picks and verifies."""

    def __init__(
        self,
        tree: Any,
        *,
        high_branches: Any = (),
        high_leaves: Any = (),
        dataset_scores: dict[int, float] | None = None,
        dimension_picks: dict[str, str] | None = None,
        choice_confidence: float = 0.9,
        uyum_p_high: float = 0.9,
        frekans_yes: float = 0.9,
        phrasing_yes: float = 0.9,
        phrasing_scores: dict[str, float] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.tree = tree
        self.high_branches = set(high_branches)
        self.high_leaves = set(high_leaves)
        self.dataset_scores = dataset_scores or {}
        self.dimension_picks = dimension_picks or {}
        self.request_picks: dict[str, str] = {}
        self.choice_confidence = choice_confidence
        self.uyum_p_high = uyum_p_high
        self.frekans_yes = frekans_yes
        self.phrasing_yes = phrasing_yes
        self.phrasing_scores = phrasing_scores or {}
        self.raise_on = raise_on
        self.stop_first = False
        self.calls: list[tuple[Any, dict[str, Any], Any]] = []
        self.choice_requests: list[str] = []

    @staticmethod
    def _prob(p_high: float) -> dict[str, Any]:
        rest = (1.0 - p_high) / 3.0
        return {
            "type": "score",
            "score": 4,
            "probabilities": [rest, rest, rest, p_high * 0.4, p_high * 0.6],
            "confidence": 0.9,
        }

    def _score(self, question_id: str) -> dict[str, Any]:
        if question_id == "uyum":
            if self.raise_on == "verify":
                raise LLMRequestError("verify stage boom")
            return self._prob(self.uyum_p_high)
        if question_id.startswith("ds-"):
            if self.raise_on == "dataset":
                raise LLMRequestError("dataset stage boom")
            return self._prob(self.dataset_scores.get(int(question_id[3:]), 0.0))
        if question_id in self.tree.branch_ids:
            if self.raise_on == "branch":
                raise LLMRequestError("branch stage boom")
            return self._prob(0.9 if question_id in self.high_branches else 0.0)
        if self.raise_on == "leaf":
            raise LLMRequestError("leaf stage boom")
        return self._prob(0.9 if question_id in self.high_leaves else 0.0)

    def _choice(self, question: dict[str, Any], state: Any) -> dict[str, Any]:
        options = list(question["criteria"])
        if (
            self.stop_first
            and len(options) == 2
            and any("Bu düzeyde kal" in option for option in options)
        ):
            return {"choice": options[0], "confidence": self.choice_confidence}
        request = state.get("istek", "") if isinstance(state, dict) else ""
        code = self.request_picks.get(request)
        if code is None:
            for label, mapped in self.dimension_picks.items():
                if label in question["instructions"]:
                    code = mapped
                    break
        if code is not None:
            for option in options:
                if code in option:
                    return {"choice": option, "confidence": self.choice_confidence}
        return {"choice": options[0], "confidence": self.choice_confidence}

    def _phrasing_score(self, instructions: str) -> float:
        for text, score in self.phrasing_scores.items():
            if text in instructions:
                return score
        return self.phrasing_yes

    def decide(self, state: Any, questions: dict[str, Any], *, prompt_ref: Any = None):
        self.calls.append((state, questions, prompt_ref))
        answers: dict[str, dict[str, Any]] = {}
        for question_id, question in questions.items():
            if question["type"] == "choice":
                self.choice_requests.append(question["instructions"])
                answers[question_id] = self._choice(question, state)
            elif question["type"] == "noul":
                if question_id == "frekans":
                    answers[question_id] = {"noul": self.frekans_yes, "confidence": 0.9}
                else:
                    answers[question_id] = {
                        "noul": self._phrasing_score(question["instructions"]),
                        "confidence": 0.9,
                    }
            else:
                answers[question_id] = self._score(question_id)
        return {"answers": answers, "usage": {}, "provider": "fake", "model": "fake"}


class FakeChat:
    """Scripted EVREN rewrite client."""

    def __init__(self, phrasings: list[str], *, fail: bool = False) -> None:
        self.phrasings = phrasings
        self.fail = fail
        self.calls: list[Any] = []

    def complete(
        self,
        messages: Any,
        *,
        response_format: Any = None,
        prompt_ref: Any = None,
        **kw: Any,
    ):
        self.calls.append((messages, response_format, prompt_ref))
        if self.fail:
            raise LLMRequestError("EVREN down")
        return SimpleNamespace(content=json.dumps({"phrasings": self.phrasings}))


class CapturingSession:
    """Captures the candidate SQL statement without running it."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> Any:
        self.statements.append(statement)

        class _Result:
            def all(self) -> list[Any]:
                return []

        return _Result()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_jev(**kwargs: Any) -> FakeJev:
    defaults: dict[str, Any] = {
        "high_branches": {BRANCH},
        "high_leaves": {LEAF},
        "dataset_scores": {1: 0.9},
        "dimension_picks": {"Gösterge": "POP"},
    }
    defaults.update(kwargs)
    return FakeJev(TREE, **defaults)


def run_search(
    monkeypatch: pytest.MonkeyPatch,
    catalog: Any,
    jev: FakeJev,
    *,
    chat: Any | None = None,
    session: FakeSession | None = None,
) -> tuple[dict[str, Any], FakeSession]:
    monkeypatch.setattr(search, "_load_candidate_datasets", catalog)
    session = session or FakeSession()
    result = search.search_series(
        session,
        REQUEST,
        jev=jev,
        chat=chat or FakeChat(["ph1", "ph2"]),
        tree=TREE,
        bodies=BODIES,
        refs=REFS,
        settings=app_settings,
        include_trace=True,
    )
    return result, session


# --------------------------------------------------------------------------- #
# Flow tests
# --------------------------------------------------------------------------- #


def test_branch_leaf_narrowing_and_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = make_dataset(1, "D1", "Konut Satışları")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev()

    result, session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "ok"
    assert len(result["strong"]) == 1
    item = result["strong"][0]
    assert item["institution"] == "tuik"
    assert item["dataset"] == "D1"
    assert item["codes"] == {"INDICATOR": "POP"}
    assert item["series_external_code"] == "D1:POP"
    assert item["frequency"] == "monthly"
    assert item["unit"] == "Kişi"
    assert item["measure"]["measure_type"] == "stok"
    assert item["rating"]["p_high"] == pytest.approx(0.9)
    assert item["arsiv"] is False
    assert item["uyumsuzluk"] == []
    assert item["series_id"] is None
    assert session.writes == 0
    assert session.scalar_calls >= 1

    # The first call carries all 24 branches.
    assert set(jev.calls[0][1]) == set(TREE.branch_order)
    # Leaf questions are scoped to the passing branch only.
    leaf_calls = [call for call in jev.calls if call[1] and set(call[1]) <= set(TREE.leaf_ids)]
    assert leaf_calls
    assert all(set(call[1]) <= set(TREE.branch_leaves[BRANCH]) for call in leaf_calls)
    assert LEAF in leaf_calls[0][1]
    # Decision-9 suffix is present on the dimension question.
    assert any(search.DIMENSION_SUFFIX.strip() in text for text in jev.choice_requests)
    # Candidate loader got the passing branch and leaf.
    assert catalog.calls[0]["leaf_ids"] == [LEAF]
    assert catalog.calls[0]["branch_ids"] == [BRANCH]
    assert catalog.calls[0]["arsiv"] == "exclude"
    # Never any observation values.
    assert "value" not in json.dumps(item)
    assert "observations" not in json.dumps(item)


def test_low_confidence_agreeing_rewrites_accept_one_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(choice_confidence=0.4)
    jev.request_picks = {REQUEST: "POP", "ph1": "POP", "ph2": "POP"}
    chat = FakeChat(["ph1", "ph2"])

    result, _session = run_search(monkeypatch, catalog, jev, chat=chat)

    assert result["status"] == "ok"
    assert result["strong"][0]["codes"]["INDICATOR"] == "POP"
    assert chat.calls
    rewrite = result["trace"]["rewrites"][0]
    assert rewrite["phrasings"] == ["ph1", "ph2"]
    assert rewrite["kept"] == ["ph1", "ph2"]


def test_low_confidence_disagreeing_rewrites_keep_two_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(choice_confidence=0.4)
    jev.request_picks = {REQUEST: "POP", "ph1": "POP", "ph2": "SALE"}
    chat = FakeChat(["ph1", "ph2"])

    result, _session = run_search(monkeypatch, catalog, jev, chat=chat)

    codes = {item["codes"]["INDICATOR"] for item in result["strong"]}
    assert codes == {"POP", "SALE"}
    assert len(result["strong"]) == 2


def test_rewrite_phrasing_that_drifted_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(choice_confidence=0.4)
    jev.request_picks = {REQUEST: "POP", "ph1": "POP"}
    jev.phrasing_scores = {"ph1": 0.9, "ph2": 0.1}
    chat = FakeChat(["ph1", "ph2"])

    result, _session = run_search(monkeypatch, catalog, jev, chat=chat)

    rewrite = result["trace"]["rewrites"][0]
    assert rewrite["phrasings"] == ["ph1", "ph2"]
    assert rewrite["kept"] == ["ph1"]
    assert result["strong"][0]["codes"]["INDICATOR"] == "POP"


def test_evren_failure_is_noted_and_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(choice_confidence=0.4)
    chat = FakeChat([], fail=True)

    result, _session = run_search(monkeypatch, catalog, jev, chat=chat)

    assert result["status"] == "ok"
    assert result["trace"]["rewrites"][0]["note"] == "rewrite_unavailable"
    assert result["trace"]["rewrites"][0]["phrasings"] == []


def test_jev_error_mid_dataset_stage_is_search_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(raise_on="dataset")

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "search_failed"
    assert result["budget_refund"] is True
    assert "dataset stage boom" in result["reason"]
    assert result["strong"] == []
    assert result["candidates"] == []


def test_frequency_mismatch_is_a_weak_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(frekans_yes=0.1)

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "no_strong_match"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["uyumsuzluk"] == ["frekans"]


def test_zero_strong_triggers_archive_second_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = make_dataset(1, "D1", "Canlı")
    archived = make_dataset(2, "D2", "Arşiv", arsiv=True)
    catalog = FakeCatalog([(live, "tuik", "TÜİK"), (archived, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.0, 2: 0.9})

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["searched_archive"] is True
    assert result["status"] == "ok"
    assert result["strong"][0]["dataset"] == "D2"
    assert result["strong"][0]["arsiv"] is True
    assert [call["arsiv"] for call in catalog.calls] == ["exclude", "only"]


def test_strong_in_first_pass_never_searches_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = make_dataset(1, "D1", "Canlı")
    archived = make_dataset(2, "D2", "Arşiv", arsiv=True)
    catalog = FakeCatalog([(live, "tuik", "TÜİK"), (archived, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.9, 2: 0.9})

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "ok"
    assert result["searched_archive"] is False
    assert result["strong"][0]["dataset"] == "D1"
    assert {call["arsiv"] for call in catalog.calls} == {"exclude"}


def test_cok_konulu_dataset_is_reached_via_branch_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cok = make_dataset(1, "COK", "Çok Konulu", cok=True)
    catalog = FakeCatalog([(cok, "tuik", "TÜİK")])
    jev = make_jev()

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["strong"][0]["dataset"] == "COK"
    assert catalog.calls[0]["branch_ids"] == [BRANCH]
    assert catalog.calls[0]["leaf_ids"] == [LEAF]


def test_series_id_reported_only_when_a_row_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev()
    session = FakeSession(series_id=42)

    result, _session = run_search(monkeypatch, catalog, jev, session=session)

    assert result["strong"][0]["series_id"] == 42


def test_candidate_sql_excludes_revision_and_archive() -> None:
    session = CapturingSession()

    search._load_candidate_datasets(session, leaf_ids=[LEAF], branch_ids=[BRANCH], arsiv="exclude")
    exclude_sql = str(session.statements[-1])
    assert "revizyon_tablosu" in exclude_sql
    assert "arsiv" in exclude_sql

    search._load_candidate_datasets(session, leaf_ids=[LEAF], branch_ids=[BRANCH], arsiv="only")
    only_sql = str(session.statements[-1])
    assert "revizyon_tablosu" in only_sql
    assert "arsiv" in only_sql


def test_candidate_sql_excludes_veri_yok_in_both_passes() -> None:
    session = CapturingSession()

    search._load_candidate_datasets(session, leaf_ids=[LEAF], branch_ids=[BRANCH], arsiv="exclude")
    assert "veri_yok" in str(session.statements[-1])

    search._load_candidate_datasets(session, leaf_ids=[LEAF], branch_ids=[BRANCH], arsiv="only")
    assert "veri_yok" in str(session.statements[-1])


def test_build_search_tool_shape() -> None:
    jev = make_jev()

    def factory() -> Any:
        return FakeSession()

    tools = search.build_search_tool(factory, jev, FakeChat(["a", "b"]), settings=app_settings)
    assert set(tools) == {"find_series"}
    schema, function = tools["find_series"]
    assert "at most 4 searches" in schema["description"]
    assert callable(function)
    assert search.SEARCH_TOOL_LIMIT == 4


# --------------------------------------------------------------------------- #
# Fix round 1: near-miss candidates
# --------------------------------------------------------------------------- #


def test_near_miss_candidates_when_nothing_is_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    d1 = make_dataset(1, "D1", "Bir")
    d2 = make_dataset(2, "D2", "İki")
    d3 = make_dataset(3, "D3", "Üç")
    catalog = FakeCatalog([(d1, "tuik", "TÜİK"), (d2, "tuik", "TÜİK"), (d3, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.31, 2: 0.23, 3: 0.12}, uyum_p_high=0.2)

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "no_strong_match"
    assert result["strong"] == []
    assert {item["dataset"] for item in result["candidates"]} == {"D1", "D2"}
    assert all(search.NEAR_MISS_MISMATCH in item["uyumsuzluk"] for item in result["candidates"])
    near = result["trace"]["near_miss"]
    assert [row["dataset"] for row in near] == ["D1", "D2"]
    assert near[0]["p_high"] == pytest.approx(0.31)
    # The dataset below the near-miss floor is never processed.
    assert "D3" not in result["trace"]["chosen"]


def test_strong_result_skips_the_near_miss_step(monkeypatch: pytest.MonkeyPatch) -> None:
    strong = make_dataset(1, "D1", "Güçlü")
    near = make_dataset(2, "D2", "Yakın")
    catalog = FakeCatalog([(strong, "tuik", "TÜİK"), (near, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.9, 2: 0.31})

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "ok"
    assert {item["dataset"] for item in result["strong"]} == {"D1"}
    assert "near_miss" not in result["trace"]


def test_near_miss_from_the_archive_pass_keeps_arsiv_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = make_dataset(1, "D1", "Canlı")
    archived = make_dataset(2, "D2", "Arşiv", arsiv=True)
    catalog = FakeCatalog([(live, "tuik", "TÜİK"), (archived, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.31, 2: 0.23}, uyum_p_high=0.2)

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["searched_archive"] is True
    assert result["status"] == "no_strong_match"
    by_dataset = {item["dataset"]: item for item in result["candidates"]}
    assert set(by_dataset) == {"D1", "D2"}
    assert by_dataset["D2"]["arsiv"] is True
    assert search.NEAR_MISS_MISMATCH in by_dataset["D2"]["uyumsuzluk"]


def test_near_miss_series_that_passes_verification_becomes_strong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_dataset(1, "D1", "Yakın")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.31})  # final verification still passes (0.9)

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "ok"
    assert result["strong"][0]["dataset"] == "D1"
    assert result["strong"][0]["uyumsuzluk"] == []
    assert search.NEAR_MISS_MISMATCH not in result["strong"][0]["uyumsuzluk"]


def test_branch_stage_failure_rates_no_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = make_dataset(1, "D1", "Konut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(high_branches=set())

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "no_strong_match"
    assert result["strong"] == []
    assert result["candidates"] == []
    assert "datasets" not in result["trace"]
    assert "near_miss" not in result["trace"]
    assert catalog.calls == []


# --------------------------------------------------------------------------- #
# Fix round 1: rewrite cache + prompt
# --------------------------------------------------------------------------- #


def test_rewrite_computed_once_for_many_low_confidence_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_multi_dim_dataset(1, "M1", "Çok Boyut")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={1: 0.9}, choice_confidence=0.4)
    chat = FakeChat(["ph1", "ph2"])

    result, _session = run_search(monkeypatch, catalog, jev, chat=chat)

    assert result["status"] == "ok"
    # Three low-confidence dimensions share one EVREN call...
    assert len(chat.calls) == 1
    # ...and one drift check per phrasing (two phrasings -> two Jev noul questions).
    drift = sum(
        1
        for _state, questions, _ref in jev.calls
        for question_id, question in questions.items()
        if question["type"] == "noul" and question_id == "q"
    )
    assert drift == 2
    assert len(result["trace"]["rewrites"]) == 1
    assert len(result["trace"]["chosen"]["M1"]) == 3


def test_rewrite_prompt_body_has_no_dimension_focus() -> None:
    body = (search.PROMPT_DIR / "search_rewrite.md").read_text(encoding="utf-8")
    assert "{boyut}" not in body
    assert "aynı anlam" in body


def test_render_tolerates_an_absent_placeholder() -> None:
    assert search._render("sabit metin", istek="x") == "sabit metin"


# --------------------------------------------------------------------------- #
# Fix round 1: single-code dimensions
# --------------------------------------------------------------------------- #


def test_single_code_dimension_skips_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = make_dataset(1, "D1", "Tek Kod", single_code=True)
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev()

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "ok"
    assert jev.choice_requests == []
    entry = result["trace"]["chosen"]["D1"]["INDICATOR"][0]
    assert entry["code"] == "POP"
    assert entry["confidence"] == 1.0
    assert entry["single_code"] is True


def test_two_code_dimension_still_asks_jev(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = make_dataset(2, "D2", "İki Kod")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev(dataset_scores={2: 0.9})

    result, _session = run_search(monkeypatch, catalog, jev)

    assert result["status"] == "ok"
    assert jev.choice_requests
    entry = result["trace"]["chosen"]["D2"]["INDICATOR"][0]
    assert "single_code" not in entry


def test_near_miss_settings_defaults() -> None:
    assert app_settings.search_near_miss_probability == 0.15
    assert app_settings.search_near_miss_datasets == 2


# --------------------------------------------------------------------------- #
# Fix round 2: search passes allow_stop=True for hierarchical dimensions
# --------------------------------------------------------------------------- #


def make_hierarchical_dataset(dataset_id: int, code: str, name: str) -> Dataset:
    dataset = Dataset(id=dataset_id, institution_id=1, external_code=code, name=name)
    dataset.attributes = {}
    dataset.dimensions = [
        DatasetDimension(
            code="NACE",
            label="Sektör",
            position=0,
            role="other",
            codes=[
                DimensionCode(
                    id=1,
                    code="C",
                    label="İmalat Sanayi",
                    attributes={"frequency": "monthly", "unit": "Adet"},
                ),
                DimensionCode(id=2, code="C10", label="Gıda", parent_code="C"),
                DimensionCode(id=3, code="C11", label="İçecek", parent_code="C"),
            ],
        )
    ]
    dataset.measure_combinations = [
        MeasureCombination(
            id=dataset_id,
            dataset_id=dataset_id,
            codes={"NACE": "C"},
            label="x",
            measure_type="akim",
            data_nature="gerceklesen",
            aggregation="toplam",
            para_birimi="yok",
            nominal_mi="nominal",
            mevsim_arindirilmis="mevsim_arindirilmamis",
            kumulatif=False,
        )
    ]
    return dataset


def _is_stop_question(question: dict[str, Any]) -> bool:
    options = list(question["criteria"])
    return len(options) == 2 and any("Bu düzeyde kal" in option for option in options)


def test_search_asks_the_stop_question_for_a_hierarchical_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = make_hierarchical_dataset(1, "H1", "Sanayi")
    catalog = FakeCatalog([(dataset, "tuik", "TÜİK")])
    jev = make_jev()
    jev.stop_first = True

    result, _session = run_search(monkeypatch, catalog, jev)

    stop_questions = [
        question
        for _state, questions, _ref in jev.calls
        for question in questions.values()
        if _is_stop_question(question)
    ]
    assert stop_questions
    assert result["strong"][0]["codes"]["NACE"] == "C"


# ---- Task 2.6: compilation datasets show their indicator labels ----------------


def _compilation(*, derleme: bool = True) -> Dataset:
    dataset = Dataset(
        institution_id=1,
        external_code="TURCAT_X",
        name="Turcat Mali Sektör",
        cok_konulu_derleme=derleme,
        attributes={},
    )
    dataset.dimensions = [
        DatasetDimension(
            code="INDICATOR",
            label="Gösterge",
            position=0,
            role="other",
            codes=[
                DimensionCode(code="1", label="Gelirler", attributes={"group": True}),
                DimensionCode(code="2", label="Merkezi Yönetim Bütçe Dengesi"),
                DimensionCode(code="3", label="Hibeler"),
                DimensionCode(code="4", label="Hibeler"),  # duplicate label
                DimensionCode(code="5", label="Veri yok", attributes={"no_data": True}),
                DimensionCode(code="6", label="Eski", attributes={"removed_at": "2026-01-01"}),
                DimensionCode(code="7", label="x" * 80),
            ],
        ),
        DatasetDimension(
            code="FREQ",
            label="Frekans",
            position=1,
            role="frequency",
            codes=[DimensionCode(code="M", label="Aylık")],
        ),
        DatasetDimension(code="TIME_PERIOD", label="Zaman", position=2, role="time", codes=[]),
    ]
    return dataset


def test_compilation_labels_skip_headers_no_data_removed_and_duplicates() -> None:
    labels = search.compilation_indicator_labels(_compilation())
    assert labels == [
        "Merkezi Yönetim Bütçe Dengesi",
        "Hibeler",
        "x" * search.COMPILATION_LABEL_CHARS,
    ]


def test_compilation_labels_are_empty_for_an_ordinary_dataset() -> None:
    assert search.compilation_indicator_labels(_compilation(derleme=False)) == []


def test_compilation_labels_are_capped() -> None:
    dataset = _compilation()
    dataset.dimensions[0].codes = [
        DimensionCode(code=str(index), label=f"Gösterge {index}") for index in range(300)
    ]
    assert len(search.compilation_indicator_labels(dataset)) == search.COMPILATION_LABEL_LIMIT


def test_dataset_text_carries_the_indicators_only_for_a_compilation() -> None:
    from app.catalog import enrich

    compilation = _compilation()
    text = search._dataset_text(
        compilation, enrich._build_view(compilation, "tuik", "TÜİK"), "tuik"
    )
    assert "gostergeler: Merkezi Yönetim Bütçe Dengesi; Hibeler" in text
    ordinary = _compilation(derleme=False)
    plain = search._dataset_text(ordinary, enrich._build_view(ordinary, "tuik", "TÜİK"), "tuik")
    assert "gostergeler" not in plain
