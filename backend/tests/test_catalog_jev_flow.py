"""Unit tests for the shared Jev flow primitives (no network, no database)."""

from __future__ import annotations

from typing import Any

import pytest

from app.catalog.jev_flow import (
    SCORE_CRITERIA,
    JevFlowError,
    Rating,
    choice_question,
    noul_question,
    parse_rating,
    parse_score,
    score_question,
    select_dimension_code,
)
from app.data.models import DatasetDimension, DimensionCode


class FakeJev:
    """Answers every choice question with the requested option."""

    def __init__(self, pick: str | None = None, confidence: float = 0.9) -> None:
        self.pick = pick
        self.confidence = confidence
        self.questions: list[dict[str, Any]] = []

    def decide(self, state: Any, questions: dict[str, Any], *, prompt_ref: Any = None):
        self.questions.append(next(iter(questions.values())))
        question = next(iter(questions.values()))
        options = list(question["criteria"])
        index = 0
        if self.pick is not None:
            index = next((i for i, option in enumerate(options) if self.pick in option), 0)
        return {"answers": {"q": {"choice": options[index], "confidence": self.confidence}}}


class StopFakeJev:
    """Scripted fake: separately picks the option for the stop question.

    The stop question is the only choice question whose criteria contains the
    "Bu düzeyde kal" / "Alt kırılıma in" pair; every other choice question uses
    ``pick``. ``stop_confidence`` lets a test make a stop answer the minimum.
    """

    def __init__(
        self,
        pick: str | None = None,
        *,
        stop_at: str | None = None,
        confidence: float = 0.9,
        stop_confidence: float | None = None,
    ) -> None:
        self.pick = pick
        self.stop_at = stop_at
        self.confidence = confidence
        self.stop_confidence = stop_confidence
        self.questions: list[dict[str, Any]] = []

    @staticmethod
    def _is_stop(question: dict[str, Any]) -> bool:
        options = list(question["criteria"])
        return len(options) == 2 and any("Bu düzeyde kal" in option for option in options)

    def decide(self, state: Any, questions: dict[str, Any], *, prompt_ref: Any = None):
        question = next(iter(questions.values()))
        self.questions.append(question)
        options = list(question["criteria"])
        if self._is_stop(question):
            index = 0
            if self.stop_at is not None:
                index = next((i for i, option in enumerate(options) if self.stop_at in option), 0)
            confidence = self.stop_confidence
            if confidence is None:
                confidence = self.confidence
            return {"answers": {"q": {"choice": options[index], "confidence": confidence}}}
        index = 0
        if self.pick is not None:
            index = next((i for i, option in enumerate(options) if self.pick in option), 0)
        return {"answers": {"q": {"choice": options[index], "confidence": self.confidence}}}


def test_parse_rating_reads_dict_probabilities() -> None:
    answer = {
        "type": "score",
        "score": 3.77,
        "confidence": 0.81,
        "probabilities": {"0": 0.01, "1": 0.01, "2": 0.04, "3": 0.08, "4": 0.86},
    }
    rating = parse_rating(answer)
    assert rating.probabilities == (0.01, 0.01, 0.04, 0.08, 0.86)
    assert rating.p_high == pytest.approx(0.94)
    expected = 1 + 0 * 0.01 + 1 * 0.01 + 2 * 0.04 + 3 * 0.08 + 4 * 0.86
    assert rating.rating_15 == pytest.approx(expected)
    assert rating.confidence == pytest.approx(0.81)


def test_parse_rating_reads_list_probabilities() -> None:
    rating = parse_rating({"score": 0.8, "probabilities": [0.05, 0.05, 0.1, 0.3, 0.5]})
    assert rating.p_high == pytest.approx(0.8)
    assert rating.probabilities == (0.05, 0.05, 0.1, 0.3, 0.5)


def test_pass_rule_at_exactly_060_and_below() -> None:
    passes = parse_rating({"probabilities": [0.1, 0.1, 0.2, 0.2, 0.4]})
    assert passes.p_high == pytest.approx(0.60)
    assert passes.passes(0.60) is True

    fails = parse_rating({"probabilities": [0.1, 0.1, 0.21, 0.2, 0.39]})
    assert fails.p_high == pytest.approx(0.59)
    assert fails.passes(0.59) is True
    assert fails.passes(0.60) is False


def test_parse_rating_falls_back_to_score_plus_one() -> None:
    rating = parse_rating({"score": 3.0})
    assert rating.rating_15 == pytest.approx(4.0)
    assert rating.probabilities == ()
    assert rating.p_high == 0.0
    assert rating.passes(0.60) is False


def test_parse_rating_without_probabilities_and_score_raises() -> None:
    with pytest.raises(JevFlowError):
        parse_rating({"confidence": 0.9})
    with pytest.raises(JevFlowError):
        parse_rating({"probabilities": [0.1, 0.2]})  # wrong length


def test_rating_passes_uses_p_high() -> None:
    rating = Rating(probabilities=(0.0, 0.0, 0.0, 0.3, 0.7), rating_15=4.7, p_high=1.0)
    assert rating.passes(0.60) is True
    assert rating.passes(1.0) is True
    assert rating.passes(1.01) is False


def test_score_question_has_five_labels_and_keeps_instructions() -> None:
    question = score_question("Bu istek 'Nüfus' dalında yer alır mı?")
    assert question["type"] == "score"
    assert question["criteria"] == list(SCORE_CRITERIA)
    assert question["instructions"] == "Bu istek 'Nüfus' dalında yer alır mı?"
    assert len(question["criteria"]) == 5


def test_choice_and_noul_question_shapes() -> None:
    choice = choice_question("pick", ["a", "b"])
    assert choice["criteria"] == {"a": "", "b": ""}
    noul = noul_question("is it?", {"evet": "yes", "hayır": "no"})
    assert noul["type"] == "noul"
    with pytest.raises(JevFlowError):
        choice_question("pick", [])
    with pytest.raises(JevFlowError):
        noul_question("is it?", {})


def _dimension() -> DatasetDimension:
    return DatasetDimension(
        code="NACE",
        label="Sektör",
        position=0,
        role="other",
        codes=[
            DimensionCode(code="A", label="Tarım"),
            DimensionCode(code="B", label="Sanayi"),
        ],
    )


def test_select_dimension_code_default_unchanged() -> None:
    jev = FakeJev()
    code, confidence, label = select_dimension_code(jev, {}, "istek", _dimension())
    assert (code, confidence, label) == ("A", 0.9, "Tarım")
    assert jev.questions[0]["instructions"] == "'istek' için Sektör seçin (üst düzey)"


def test_select_dimension_code_appends_instruction_suffix() -> None:
    jev = FakeJev()
    select_dimension_code(
        jev,
        {},
        "istek",
        _dimension(),
        instruction_suffix=" SUFFIX",
    )
    assert jev.questions[0]["instructions"].endswith("(üst düzey) SUFFIX")


def test_select_dimension_code_interpolates_instruction_template() -> None:
    jev = FakeJev()
    select_dimension_code(
        jev,
        {},
        "istek",
        _dimension(),
        instruction_template="{istek} için uygun {boyut} kodunu seçin.",
        instruction_suffix=" SUFFIX",
    )
    assert jev.questions[0]["instructions"] == ("istek için uygun Sektör kodunu seçin. SUFFIX")


def test_parse_score_ignores_choice_shape() -> None:
    assert parse_score({"noul": 0.87})[0] == pytest.approx(0.87)
    with pytest.raises(JevFlowError):
        parse_score({"other": 1})


# --------------------------------------------------------------------------- #
# allow_stop (search-only "stay at this level" question)
# --------------------------------------------------------------------------- #


def _hierarchy() -> DatasetDimension:
    return DatasetDimension(
        code="NACE",
        label="Sektör",
        position=0,
        role="other",
        codes=[
            DimensionCode(code="C", label="İmalat Sanayi"),
            DimensionCode(code="C10", label="Gıda", parent_code="C"),
            DimensionCode(code="C11", label="İçecek", parent_code="C"),
        ],
    )


def _stop_questions(jev: Any) -> list[dict[str, Any]]:
    return [q for q in jev.questions if StopFakeJev._is_stop(q)]


def test_allow_stop_can_stay_on_the_parent_code() -> None:
    jev = StopFakeJev(stop_at="Bu düzeyde kal")
    code, confidence, label = select_dimension_code(
        jev, {}, "tüm imalat sanayi", _hierarchy(), allow_stop=True
    )
    assert (code, confidence, label) == ("C", 0.9, "İmalat Sanayi")
    assert len(_stop_questions(jev)) == 1
    # Stopping asks no children question: only the root choice + the stop question.
    assert len(jev.questions) == 2


def test_allow_stop_descend_reaches_the_child() -> None:
    jev = StopFakeJev(pick="C10", stop_at="Alt kırılıma in")
    code, confidence, label = select_dimension_code(
        jev, {}, "gıda üretimi", _hierarchy(), allow_stop=True
    )
    assert (code, confidence, label) == ("C10", 0.9, "Gıda")
    # Only C has children, so exactly one stop question is asked, then descend.
    assert len(_stop_questions(jev)) == 1
    assert len(jev.questions) == 3  # root choice, stop question, children choice


def test_allow_stop_confidence_is_the_min_over_all_answers() -> None:
    jev = StopFakeJev(stop_at="Alt kırılıma in", confidence=0.9, stop_confidence=0.5)
    _code, confidence, _label = select_dimension_code(
        jev, {}, "istek", _hierarchy(), allow_stop=True
    )
    assert confidence == pytest.approx(0.5)


def test_leaf_code_never_gets_a_stop_question() -> None:
    leaf = DatasetDimension(
        code="NACE",
        label="Sektör",
        position=0,
        role="other",
        codes=[
            DimensionCode(code="C10", label="Gıda", parent_code="C"),
            DimensionCode(code="C11", label="İçecek", parent_code="C"),
        ],
    )
    jev = StopFakeJev(pick="C10")
    select_dimension_code(jev, {}, "istek", leaf, allow_stop=True)
    assert _stop_questions(jev) == []


def test_allow_stop_false_is_byte_for_byte_the_old_sequence() -> None:
    baseline = FakeJev(pick="C10")
    select_dimension_code(baseline, {}, "istek", _hierarchy())
    with_stop = StopFakeJev(pick="C10")
    select_dimension_code(with_stop, {}, "istek", _hierarchy())

    baseline_instructions = [q["instructions"] for q in baseline.questions]
    with_stop_instructions = [q["instructions"] for q in with_stop.questions]
    assert with_stop_instructions == baseline_instructions
    assert len(with_stop.questions) == len(baseline.questions)
    assert _stop_questions(with_stop) == []


def test_allow_stop_descend_chunks_more_than_255_children() -> None:
    children = [
        DimensionCode(code=f"C{index}", label=f"Alt {index}", parent_code="C")
        for index in range(600)
    ]
    dimension = DatasetDimension(
        code="NACE",
        label="Sektör",
        position=0,
        role="other",
        codes=[DimensionCode(code="C", label="İmalat Sanayi"), *children],
    )
    jev = StopFakeJev(stop_at="Alt kırılıma in")
    select_dimension_code(jev, {}, "istek", dimension, allow_stop=True)
    # The stop question is separate, so the chunking questions are still <= 255.
    code_questions = [q for q in jev.questions if not StopFakeJev._is_stop(q)]
    assert len(code_questions) >= 3
    assert all(len(q["criteria"]) <= 255 for q in code_questions)
