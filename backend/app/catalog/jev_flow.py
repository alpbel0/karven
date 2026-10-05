"""Reusable Jev decision-flow primitives (Task 2.4).

The pieces here started life in :mod:`app.catalog.linking` (the Turcat ->
databrowser2 prototype). They are moved here unchanged so the catalog series
search (:mod:`app.catalog.search`) can share exactly the same question shapes,
answer parsers and hierarchical code selection.

Nothing in this module touches the database: the calls are pure question
building, tolerant answer parsing and the ``choice``/``noul``/``score`` flow
against the :class:`JevDecider` protocol.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.data.models import DatasetDimension

logger = logging.getLogger("app.catalog.jev_flow")

#: Jev ``choice`` accepts at most this many options in one question.
MAX_CHOICE_OPTIONS = 255
#: Defensive cap on the hierarchy depth when selecting a code.
MAX_HIERARCHY_DEPTH = 32

CHOICE_TYPE = "choice"
NOUL_TYPE = "noul"
SCORE_TYPE = "score"

#: The exact 1-5 labels a live Jev ``score`` question expects (probed 2026-10-04).
SCORE_CRITERIA = (
    "1 - hiç uymuyor",
    "2 - zayıf uyuyor",
    "3 - kısmen uyuyor",
    "4 - iyi uyuyor",
    "5 - tam uyuyor",
)
#: Number of rating levels (1-5).
SCORE_LEVELS = 5


class JevFlowError(Exception):
    """A Jev decision-flow failure (bad Jev answer, no candidates, ...)."""


class JevDecider(Protocol):
    """The subset of :class:`app.llm.jev.JevClient` this module needs."""

    def decide(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        prompt_ref: Any = None,
    ) -> dict[str, Any]:
        """Ask Jev a set of questions and return answers plus usage/provider."""


@dataclass(frozen=True)
class ChoiceAnswer:
    """A parsed Jev choice answer."""

    index: int
    label: str
    confidence: float


@dataclass(frozen=True)
class Rating:
    """A parsed 1-5 Jev ``score`` answer.

    ``p_high`` is ``p(rating 4) + p(rating 5)`` and the user's pass rule is
    ``p_high >= threshold``. ``rating_15`` is the expected rating on the 1-5
    scale (``1 + sum(i * p_i)``); it is only a fallback ordering signal and never
    the pass decision.
    """

    probabilities: tuple[float, ...]
    rating_15: float
    p_high: float
    confidence: float = 1.0

    def passes(self, threshold: float) -> bool:
        """True when the combined 4-5 probability clears ``threshold``."""
        return self.p_high >= threshold


def score_question(instructions: str) -> dict[str, Any]:
    """A Jev ``score`` question with the five verified 1-5 criteria labels.

    The thing being scored must be written inside ``instructions`` (Task 2.3
    live lesson: criteria-only names score ~0.94 for everything).
    """
    return {"type": SCORE_TYPE, "instructions": instructions, "criteria": list(SCORE_CRITERIA)}


def choice_question(instructions: str, options: Sequence[str]) -> dict[str, Any]:
    """A Jev ``choice`` question with at most 255 options.

    Jev's ``choice`` question takes its options as a ``criteria`` object keyed by
    the option label (verified live 2026-09-30). Option labels must be unique.
    """
    if not options:
        raise JevFlowError("choice question needs at least one option")
    if len(options) > MAX_CHOICE_OPTIONS:
        raise JevFlowError(f"choice question has {len(options)} options (max {MAX_CHOICE_OPTIONS})")
    criteria = {option: "" for option in options}
    if len(criteria) != len(options):
        raise JevFlowError("choice question options must be unique")
    return {"type": CHOICE_TYPE, "instructions": instructions, "criteria": criteria}


def noul_question(instructions: str, criteria: dict[str, str]) -> dict[str, Any]:
    """A Jev ``noul`` verification question returning a 0..1 match score."""
    if not criteria:
        raise JevFlowError("noul question needs criteria")
    return {"type": NOUL_TYPE, "instructions": instructions, "criteria": dict(criteria)}


def parse_choice(answer: dict[str, Any], options: Sequence[str]) -> ChoiceAnswer:
    """Parse a Jev choice answer tolerantly (label, index or free text)."""
    if not isinstance(answer, dict):
        raise JevFlowError(f"choice answer is not an object: {answer!r}")
    raw = answer.get("choice", answer.get("selected", answer.get("value")))
    if raw is None:
        for candidate in ("index", "position", "answer"):
            if candidate in answer:
                raw = answer[candidate]
                break
    if raw is None:
        raise JevFlowError(f"choice answer has no choice: {answer!r}")
    index: int | None = None
    if isinstance(raw, bool):
        raise JevFlowError(f"invalid choice {raw!r}")
    if isinstance(raw, int):
        index = raw
    elif isinstance(raw, float):
        index = int(raw)
    else:
        text = str(raw).strip()
        if text.isdigit():
            index = int(text)
        else:
            index = _match_option(text, options)
    if index is None or index < 0 or index >= len(options):
        raise JevFlowError(f"choice answer {raw!r} is not one of the options")
    confidence = _confidence(answer, default=1.0)
    return ChoiceAnswer(index=index, label=options[index], confidence=confidence)


def parse_score(answer: dict[str, Any]) -> tuple[float, float]:
    """Parse a ``noul``/``score`` answer into ``(score, confidence)``."""
    if not isinstance(answer, dict):
        raise JevFlowError(f"noul answer is not an object: {answer!r}")
    raw = answer.get("score", answer.get("noul", answer.get("value", answer.get("probability"))))
    if raw is None:
        raise JevFlowError(f"noul answer has no score: {answer!r}")
    try:
        score = float(raw)
    except (TypeError, ValueError) as exc:
        raise JevFlowError(f"noul score {raw!r} is not numeric") from exc
    return score, _confidence(answer, default=score)


def parse_rating(answer: dict[str, Any]) -> Rating:
    """Parse a Jev ``score`` answer into a :class:`Rating`.

    ``probabilities`` may be the live object keyed ``"0".."4"`` or a plain list
    of five floats. When the probabilities are missing, ``score + 1`` becomes the
    fallback ``rating_15`` (and ``p_high`` is 0, so it never passes the rule);
    when both are missing the flow error is raised.
    """
    if not isinstance(answer, dict):
        raise JevFlowError(f"score answer is not an object: {answer!r}")
    raw = answer.get("probabilities")
    if raw is not None:
        probabilities = _parse_probabilities(raw)
        rating_15 = 1.0 + sum(index * value for index, value in enumerate(probabilities))
        p_high = probabilities[3] + probabilities[4]
        return Rating(
            probabilities=tuple(probabilities),
            rating_15=rating_15,
            p_high=p_high,
            confidence=_confidence(answer, default=1.0),
        )
    score = answer.get("score", answer.get("value"))
    if score is None:
        raise JevFlowError(f"score answer has neither probabilities nor score: {answer!r}")
    try:
        value = float(score)
    except (TypeError, ValueError) as exc:
        raise JevFlowError(f"score {score!r} is not numeric") from exc
    return Rating(
        probabilities=(),
        rating_15=value + 1.0,
        p_high=0.0,
        confidence=_confidence(answer, default=1.0),
    )


def _parse_probabilities(raw: Any) -> list[float]:
    if isinstance(raw, dict):
        try:
            return [float(raw[str(index)]) for index in range(SCORE_LEVELS)]
        except (KeyError, TypeError, ValueError) as exc:
            raise JevFlowError(f"score probabilities are incomplete: {raw!r}") from exc
    if isinstance(raw, (list, tuple)):
        if len(raw) != SCORE_LEVELS:
            raise JevFlowError(f"score probabilities must have {SCORE_LEVELS} entries: {raw!r}")
        try:
            return [float(value) for value in raw]
        except (TypeError, ValueError) as exc:
            raise JevFlowError(f"score probabilities are not numeric: {raw!r}") from exc
    raise JevFlowError(f"score probabilities have an unsupported shape: {raw!r}")


def _confidence(answer: dict[str, Any], *, default: float) -> float:
    raw = answer.get("confidence")
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _match_option(text: str, options: Sequence[str]) -> int | None:
    lowered = text.casefold()
    for index, option in enumerate(options):
        if option.casefold() == lowered:
            return index
    for index, option in enumerate(options):
        if lowered in option.casefold() or option.casefold() in lowered:
            return index
    return None


def _ask(
    jev: JevDecider,
    state: Any,
    question: dict[str, Any],
    *,
    prompt_ref: Any = None,
) -> dict[str, Any]:
    result = jev.decide(state, {"q": question}, prompt_ref=prompt_ref)
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or "q" not in answers:
        raise JevFlowError(f"Jev response missing an answer for {question!r}")
    answer = answers["q"]
    if not isinstance(answer, dict):
        raise JevFlowError(f"Jev answer is not an object: {answer!r}")
    return answer


def narrow_choice(
    jev: JevDecider,
    state: Any,
    instruction: str,
    items: Sequence[Any],
    *,
    labeler: Callable[[Any], str],
    prompt_ref: Any = None,
) -> tuple[Any, float]:
    """Pick one item with choice questions, narrowing lists over 255 options.

    Long lists are chunked into groups of at most 255 and the ``labeler`` names
    each chunk by its first/last member; the flow then recurses into the chosen
    chunk. This keeps every question within Jev's option limit.
    """
    if not items:
        raise JevFlowError(f"no candidates for: {instruction}")
    if len(items) <= MAX_CHOICE_OPTIONS:
        options = [labeler(item) for item in items]
        answer = _ask(jev, state, choice_question(instruction, options), prompt_ref=prompt_ref)
        parsed = parse_choice(answer, options)
        return items[parsed.index], parsed.confidence
    chunks = [
        items[index : index + MAX_CHOICE_OPTIONS]
        for index in range(0, len(items), MAX_CHOICE_OPTIONS)
    ]
    options = [
        f"{labeler(chunk[0])} … {labeler(chunk[-1])} ({len(chunk)} seçenek)" for chunk in chunks
    ]
    answer = _ask(jev, state, choice_question(instruction, options), prompt_ref=prompt_ref)
    parsed = parse_choice(answer, options)
    return narrow_choice(
        jev, state, instruction, chunks[parsed.index], labeler=labeler, prompt_ref=prompt_ref
    )


def _code_label(dimension: DatasetDimension) -> Callable[[Any], str]:
    def label(code: Any) -> str:
        return f"{code.label} [{code.code}]"

    return label


def _active_codes(dimension: DatasetDimension) -> list[Any]:
    return [code for code in dimension.codes if not (code.attributes or {}).get("removed_at")]


def _dimension_instruction(
    template: str | None, request: str, dimension_label: str, *, child_of: str | None
) -> str:
    if template is not None:
        return template.replace("{istek}", request).replace("{boyut}", dimension_label)
    if child_of is None:
        return f"'{request}' için {dimension_label} seçin (üst düzey)"
    return f"'{request}' için {child_of} içinden {dimension_label} seçin"


def _stop_question(
    request: str, dimension_label: str, chosen: Any
) -> tuple[dict[str, Any], str, str]:
    """The "stay at this level or descend" binary question for a parent code."""
    stay = f"Bu düzeyde kal: {chosen.label} [{chosen.code}] (alt kırılım gerekmiyor)"
    descend = f"Alt kırılıma in: {chosen.label} [{chosen.code}] içindeki alt kodlar"
    instructions = (
        f"'{request}' isteği {dimension_label} için yalnızca '{chosen.label}' düzeyinde "
        f"(genel/toplam) mi, yoksa alt kırılımlardan birini mi istiyor?"
    )
    return choice_question(instructions, [stay, descend]), stay, descend


def select_dimension_code(
    jev: JevDecider,
    state: Any,
    request: str,
    dimension: DatasetDimension,
    *,
    prompt_ref: Any = None,
    instruction_suffix: str = "",
    instruction_template: str | None = None,
    allow_stop: bool = False,
) -> tuple[str, float, str]:
    """Choose one code of ``dimension`` hierarchically (parent level first).

    ``instruction_suffix`` is appended verbatim to every code-choice question;
    the search passes the "unmentioned breakdown -> pick the total" clause.
    ``instruction_template`` (with ``{istek}``/``{boyut}``) replaces the built-in
    stem when given. The default output is identical to the original linker
    behaviour.

    With ``allow_stop=True`` (search only) every parent with children is first
    given a binary "stay at this level / descend" question, so a request for a
    whole section (e.g. NACE ``C``) can stop on the parent code. The stop question
    carries no ``instruction_suffix`` (that clause belongs to the code choices).
    """
    codes = _active_codes(dimension)
    if not codes:
        raise JevFlowError(f"dimension {dimension.code!r} has no codes")
    code_set = {code.code for code in codes}
    roots = [code for code in codes if not code.parent_code or code.parent_code not in code_set]
    if not roots:
        roots = codes
    root_instruction = _dimension_instruction(
        instruction_template, request, str(dimension.label), child_of=None
    )
    chosen, confidence = narrow_choice(
        jev,
        state,
        f"{root_instruction}{instruction_suffix}",
        roots,
        labeler=_code_label(dimension),
        prompt_ref=prompt_ref,
    )
    confidences = [confidence]
    depth = 0
    while depth < MAX_HIERARCHY_DEPTH:
        depth += 1
        children = [code for code in codes if code.parent_code == chosen.code]
        if not children:
            break
        if allow_stop:
            question, stay, _descend = _stop_question(request, str(dimension.label), chosen)
            answer = _ask(jev, state, question, prompt_ref=prompt_ref)
            parsed = parse_choice(answer, [stay, _descend])
            confidences.append(parsed.confidence)
            if parsed.index == 0:
                break
        child_instruction = _dimension_instruction(
            instruction_template, request, str(dimension.label), child_of=str(chosen.label)
        )
        chosen, confidence = narrow_choice(
            jev,
            state,
            f"{child_instruction}{instruction_suffix}",
            children,
            labeler=_code_label(dimension),
            prompt_ref=prompt_ref,
        )
        confidences.append(confidence)
    return str(chosen.code), min(confidences), str(chosen.label)


__all__ = [
    "CHOICE_TYPE",
    "MAX_CHOICE_OPTIONS",
    "MAX_HIERARCHY_DEPTH",
    "NOUL_TYPE",
    "SCORE_CRITERIA",
    "SCORE_LEVELS",
    "SCORE_TYPE",
    "ChoiceAnswer",
    "JevDecider",
    "JevFlowError",
    "Rating",
    "choice_question",
    "narrow_choice",
    "noul_question",
    "parse_choice",
    "parse_rating",
    "parse_score",
    "score_question",
    "select_dimension_code",
]
