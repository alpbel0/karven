"""The Jev enrichment pass (Task 2.2b).

The deterministic rules (Task 2.2a) fill what they can and leave pending markers;
this module asks Jev the remaining questions and persists the answers with
``*_method='jev'``. Thresholds: a confidence >= 0.70 is accepted automatically,
<= 0.30 is a definite "no", and the ambiguous middle goes to ``review`` for a
human. Rule values always win; Jev/manual/reviewed values are never overwritten.

Steps (each rerunnable and resumable; only rows still pending are touched):

1. ``dims``     - is a dimension a measure dimension? then re-enumerate.
2. ``types``    - measure type + data nature of each combination.
3. ``currency`` - currency + nominal/reel of amount combinations.
4. ``periods``  - is a candidate group one statistic split by period/base year?
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.catalog import enrich, linking
from app.catalog import enrich_rules as rules
from app.catalog.tree import ConceptTree, load_tree
from app.data.models import (
    Dataset,
    DatasetDimension,
    Institution,
    MeasureCombination,
)
from app.llm.errors import LLMOutputError
from app.prompts.errors import PromptNotFoundError
from app.prompts.ref import PromptRef
from app.prompts.service import activate, add_version, get_active

logger = logging.getLogger("app.catalog.enrich.jev")

PROMPT_DIR = Path(__file__).parent / "prompts"

#: Logical step name -> prompt key.
PROMPT_KEYS = {
    "measure_dim": "catalog.enrich.measure_dim",
    "measure_type": "catalog.enrich.measure_type",
    "data_nature": "catalog.enrich.data_nature",
    "currency": "catalog.enrich.currency",
    "nominal": "catalog.enrich.nominal",
    "period_series": "catalog.enrich.period_series",
}
_DEFAULT_PROMPTS = (
    ("catalog.enrich.measure_dim", "measure_dim.md"),
    ("catalog.enrich.measure_type", "measure_type.md"),
    ("catalog.enrich.data_nature", "data_nature.md"),
    ("catalog.enrich.currency", "currency.md"),
    ("catalog.enrich.nominal", "nominal.md"),
    ("catalog.enrich.period_series", "period_series.md"),
)

#: Auto-accept / auto-reject thresholds live in :mod:`app.catalog.enrich` so the
#: rules merge and the status recompute share one source of truth.
ACCEPT = enrich.ACCEPT_CONFIDENCE
REJECT = enrich.REJECT_CONFIDENCE
CONSECUTIVE_FAILURE_LIMIT = 20
PROGRESS_EVERY = 200

CURRENCY_IDS = ("TRY", "USD", "EUR", "XDR", "diger", "yok")

_STEPS = ("dims", "types", "currency", "periods")


class JevDecider(Protocol):
    """The subset of :class:`app.llm.jev.JevClient` this module uses."""

    def decide(
        self, state: Any, questions: dict[str, Any], *, prompt_ref: PromptRef | None = None
    ) -> dict[str, Any]:
        """Ask Jev a set of questions."""


class SessionFactory(Protocol):
    """A callable returning a session context manager."""

    def __call__(self) -> Any:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------- #
# Prompt seeding
# --------------------------------------------------------------------------- #


def _seed_missing(active_keys: set[str], add: Callable[[str, str], None]) -> None:
    """Seed the defaults only for keys not already active (pure/two-callable)."""
    for key, filename in _DEFAULT_PROMPTS:
        if key in active_keys:
            continue
        body = (PROMPT_DIR / filename).read_text(encoding="utf-8")
        add(key, body)


def ensure_default_prompts(session: Session) -> None:
    """Seed the default bodies for keys with no active version (never overwrite)."""
    active: set[str] = set()
    for key, _filename in _DEFAULT_PROMPTS:
        try:
            get_active(session, key)
            active.add(key)
        except PromptNotFoundError:
            pass

    def add(key: str, body: str) -> None:
        version = add_version(session, key=key, body=body, note="Task 2.2b default")
        activate(session, key=key, version=version.version)

    _seed_missing(active, add)
    session.flush()


def prompt_refs(session: Session) -> dict[str, PromptRef]:
    """The active prompt refs keyed by logical step name."""
    refs: dict[str, PromptRef] = {}
    for name, key in PROMPT_KEYS.items():
        view = get_active(session, key)
        refs[name] = PromptRef(key=view.key, version=view.version, checksum=view.checksum)
    return refs


def default_jev_factory() -> JevDecider:
    """Build the real decision client (TypeSafe first, OpenRouter fallback)."""
    from app.config import settings
    from app.llm.jev import JevClient
    from app.llm.raw_log import build_raw_logger

    raw_logger = build_raw_logger(settings) if settings.minio_bucket_raw else None
    return JevClient(settings, raw_logger=raw_logger)


# --------------------------------------------------------------------------- #
# Answer parsing
# --------------------------------------------------------------------------- #


def _answer_for(result: dict[str, Any], question_id: str) -> dict[str, Any]:
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or question_id not in answers:
        raise LLMOutputError(f"Jev response missing an answer for {question_id!r}")
    answer = answers[question_id]
    if not isinstance(answer, dict):
        raise LLMOutputError(f"Jev answer {question_id!r} is not an object: {answer!r}")
    return answer


def _choice_result(answer: dict[str, Any], valid: Iterable[str]) -> tuple[str, float]:
    valid_set = set(valid)
    raw = answer.get("choice", answer.get("answer", answer.get("index")))
    if raw is None:
        raise LLMOutputError(f"choice answer has no choice: {answer!r}")
    value = str(raw)
    if value not in valid_set:
        raise LLMOutputError(f"choice {value!r} is not one of {sorted(valid_set)}")
    confidence = answer.get("confidence")
    try:
        return value, float(confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        probabilities = answer.get("probabilities") or {}
        try:
            return value, max(float(value) for value in probabilities.values())
        except (TypeError, ValueError):
            return value, 1.0


def _decisiveness(score: float) -> float:
    """How decisive a yes-probability is: ``max(p, 1-p)``.

    Jev's noul answers carry no ``confidence`` (``{"noul": 0.16}``); the raw
    ``0.16`` is the yes-probability, not a low confidence. A clear "no" is 0.84
    decisive, so the stored confidence must be ``max(p, 1-p)``.
    """
    return max(score, 1.0 - score)


def _noul_result(answer: dict[str, Any]) -> tuple[float, float]:
    """Parse a noul answer to ``(yes_probability, decisiveness_confidence)``."""
    score, _confidence = linking.parse_score(answer)
    return score, _decisiveness(score)


def dim_decision(score: float) -> bool | None:
    """``True``/``False`` above/below the thresholds, ``None`` in the middle."""
    if score >= ACCEPT:
        return True
    if score <= REJECT:
        return False
    return None


def nominal_value(score: float) -> str | None:
    """``reel``/``nominal`` above/below the thresholds, ``None`` in the middle."""
    if score >= ACCEPT:
        return "reel"
    if score <= REJECT:
        return "nominal"
    return None


def period_decision(score: float) -> str | None:
    """``yes``/``no`` above/below the thresholds, ``None`` in the middle."""
    if score >= ACCEPT:
        return "yes"
    if score <= REJECT:
        return "no"
    return None


def questions_for_combination(
    combination: MeasureCombination, tree: ConceptTree, bodies: dict[str, str]
) -> dict[str, Any]:
    """Only the still-missing questions (resumability)."""
    questions: dict[str, Any] = {}
    if combination.measure_type is None:
        questions["tur"] = type_question(bodies["measure_type"], tree)
    if combination.data_nature is None:
        questions["nitelik"] = nature_question(bodies["data_nature"], tree)
    return questions


def questions_for_currency(
    combination: MeasureCombination, bodies: dict[str, str]
) -> dict[str, Any]:
    attributes = combination.attributes or {}
    questions: dict[str, Any] = {}
    if attributes.get("currency_pending"):
        questions["para"] = currency_question(bodies["currency"])
    if attributes.get("nominal_pending"):
        questions["nominal"] = nominal_question(bodies["nominal"])
    return questions


# --------------------------------------------------------------------------- #
# State and question builders (pure; unit-tested)
# --------------------------------------------------------------------------- #


def category_text(dataset: Dataset, institution_code: str) -> str:
    """Full category path for TCMB/HMB, else the source category."""
    if institution_code in {"tcmb", "hmb"}:
        path = (dataset.attributes or {}).get("category_path")
        if isinstance(path, list):
            titles = [
                str(entry["title"])
                for entry in path
                if isinstance(entry, dict) and entry.get("title")
            ]
            if titles:
                return " > ".join(titles)
    return dataset.source_category or ""


def _clean_state(state: dict[str, Any]) -> dict[str, Any]:
    """Drop empty strings/None so the Jev state carries only real fields."""
    return {
        key: value
        for key, value in state.items()
        if value is not None and value != "" and value != []
    }


def build_dim_state(
    dataset: Dataset, institution_code: str, dimension: DatasetDimension
) -> dict[str, Any]:
    labels = [code.label for code in dimension.codes]
    return _clean_state(
        {
            "kurum": institution_code,
            "veri_seti": dataset.name,
            "kategori": category_text(dataset, institution_code),
            "kirilim_kodu": dimension.code,
            "kirilim_adi": dimension.label,
            "ornek_kodlar": "; ".join(labels[:15]),
            "kod_sayisi": len(labels),
        }
    )


def _combination_unit(view: rules.DatasetView, codes: dict[str, Any]) -> str:
    dimensions = {dimension.code: dimension for dimension in view.dimensions}
    parts: list[str] = []
    for dimension_code, code_value in codes.items():
        dimension = dimensions.get(dimension_code)
        if dimension is None:
            continue
        matched = next((code for code in dimension.codes if code.code == code_value), None)
        unit = (matched.attributes or {}).get("unit") if matched is not None else None
        if unit:
            parts.append(str(unit))
        if dimension.code in {"UNIT_MEASURE", "OLCU_BIRIMI"} and matched is not None:
            parts.append(matched.label)
    dataset_unit = view.attributes.get("unit")
    if dataset_unit:
        parts.append(str(dataset_unit))
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return " | ".join(seen)


def build_type_state(
    dataset: Dataset,
    institution_code: str,
    view: rules.DatasetView,
    combination: MeasureCombination,
) -> dict[str, Any]:
    source_description = (dataset.attributes or {}).get("source_description")
    return _clean_state(
        {
            "kurum": institution_code,
            "veri_seti": dataset.name,
            "kategori": category_text(dataset, institution_code),
            "gosterge": combination.label,
            "birim": _combination_unit(view, dict(combination.codes or {})),
            "frekans": ", ".join(rules.dataset_frequencies(view)),
            "kaynak_aciklamasi": str(source_description)[:300] if source_description else "",
        }
    )


def build_currency_state(
    dataset: Dataset,
    institution_code: str,
    view: rules.DatasetView,
    combination: MeasureCombination,
) -> dict[str, Any]:
    return _clean_state(
        {
            "kurum": institution_code,
            "veri_seti": dataset.name,
            "gosterge": combination.label,
            "birim": _combination_unit(view, dict(combination.codes or {})),
        }
    )


def build_period_state(
    members: list[Dataset], group_key: str
) -> dict[str, Any]:
    lines: list[str] = []
    for dataset in members:
        start = dataset.coverage_start.isoformat() if dataset.coverage_start else "?"
        end = dataset.coverage_end.isoformat() if dataset.coverage_end else "?"
        lines.append(f"{dataset.name} [{dataset.external_code}] ({start}–{end})")
    return {"grup": group_key, "uyeler": lines}


def _period_group_decided(members: list[Dataset], group_key: str) -> bool:
    """True when a period-series group is already settled and must not be re-asked.

    Manual/reviewed members or a member whose period series was already decided
    (accepted yes / rejected no) settle the whole group.
    """
    for member in members:
        attributes = member.attributes or {}
        if attributes.get("period_series_reviewed") or attributes.get("period_series_manual"):
            return True
        if attributes.get("period_series_method") == "manual":
            return True
        if attributes.get("period_series_rejected"):
            return True
        if member.donem_serisi and member.donem_serisi_grubu == group_key:
            return True
    return False


def dim_question(instructions: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {
            "evet": "Evet, bu kırılım ölçüyü belirler.",
            "hayir": "Hayır, bu kırılım ayrımdır.",
        },
    }


def type_question(instructions: str, tree: ConceptTree) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {mid: measure.tanim for mid, measure in tree.measure_types.items()},
    }


def nature_question(instructions: str, tree: ConceptTree) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {nid: nature.tanim for nid, nature in tree.data_natures.items()},
    }


def currency_question(instructions: str) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {
            "TRY": "Türk lirası",
            "USD": "ABD doları",
            "EUR": "Euro",
            "XDR": "SDR (IMF özel çekme hakkı)",
            "diger": "Diğer para birimi",
            "yok": "Para birimi yok",
        },
    }


def nominal_question(instructions: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {
            "evet": "Evet, reel/sabit fiyatlı/zincirlenmiş hacim.",
            "hayir": "Hayır, cari fiyatlı/nominal.",
        },
    }


def period_question(instructions: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {
            "evet": "Evet, aynı istatistiğin dönem/baz yılı parçaları.",
            "hayir": "Hayır, farklı istatistikler.",
        },
    }


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #


def _load_dataset(session: Session, dataset_id: int) -> tuple[Dataset, str, str]:
    row = session.execute(
        sa.select(Dataset, Institution.code, Institution.name)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(Dataset.id == dataset_id)
        .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
    ).one()
    dataset, code, name = row
    return dataset, code, name


def _load_view(session: Session, dataset_id: int) -> tuple[Dataset, rules.DatasetView]:
    dataset, code, name = _load_dataset(session, dataset_id)
    return dataset, enrich._build_view(dataset, code, name)


class _ViewCache:
    """Thread-safe per-step cache of a dataset's immutable :class:`DatasetView`.

    A big TCMB dataset has thousands of combinations; building its view (which
    walks every dimension code) once per combination is O(n²). The collector
    builds each view once and hands it to the worker items.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._views: dict[int, rules.DatasetView] = {}

    def get(
        self, dataset: Dataset, institution_code: str, institution_name: str
    ) -> rules.DatasetView:
        with self._lock:
            view = self._views.get(dataset.id)
            if view is None:
                view = enrich._build_view(dataset, institution_code, institution_name)
                self._views[dataset.id] = view
            return view


def recompute_touched_flags(
    session_factory: SessionFactory,
    dataset_ids: Iterable[int],
    *,
    step: str,
    out: Callable[[str], None] = print,
) -> int:
    """Recompute the array flags once per touched dataset; return failure count.

    Called in the main thread after a step's pool has committed, so no worker
    runs an O(n) flag scan per combination.
    """
    errors = 0
    for dataset_id in sorted(set(dataset_ids)):
        try:
            with session_factory() as session:
                dataset = session.get(Dataset, dataset_id)
                if dataset is None:
                    continue
                enrich.recompute_dataset_flags(session, dataset)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - counted, not silent
            errors += 1
            logger.exception("flag recompute failed for dataset %s", dataset_id)
            out(f"error step={step} dataset={dataset_id} {type(exc).__name__}: {exc}")
    return errors


def refresh_statuses(
    session: Session,
    *,
    institution: str | None = None,
    dataset: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Recompute status/markers from stored answers with the current thresholds.

    No Jev call. It also repairs a value stored with the OLD noul meaning: a raw
    ``{"noul": 0.16}`` is a decisive "no" (confidence 0.84), not a 0.16
    confidence. It re-decides a dim/nominal/period left in review, and rewrites
    the stored confidence for every Jev noul answer it sees.
    """
    counts = {"combinations": 0, "dims": 0, "periods": 0, "nominal": 0}

    dim_stmt = (
        sa.select(DatasetDimension)
        .join(Dataset, DatasetDimension.dataset_id == Dataset.id)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(DatasetDimension.attributes["measure_jev"].astext.isnot(None))
        .order_by(DatasetDimension.id)
    )
    if institution:
        dim_stmt = dim_stmt.where(Institution.code == institution)
    if dataset:
        dim_stmt = dim_stmt.where(Dataset.external_code == dataset)
    if limit:
        dim_stmt = dim_stmt.limit(limit)
    for dimension in session.scalars(dim_stmt).all():
        attributes = dict(dimension.attributes or {})
        score = attributes.get("measure_score")
        raw = attributes.get("measure_jev")
        if not isinstance(score, (int, float)) and isinstance(raw, dict):
            score, _confidence = linking.parse_score(raw)
        if isinstance(score, (int, float)):
            attributes["measure_score"] = float(score)
            attributes["measure_confidence"] = _decisiveness(float(score))
        if dimension.is_measure is None and attributes.get("measure_review"):
            decision = dim_decision(float(score)) if isinstance(score, (int, float)) else None
            if decision is not None:
                dimension.is_measure = decision
                dimension.measure_source = "jev"
                attributes.pop("measure_review", None)
                counts["dims"] += 1
        dimension.attributes = attributes

    combo_stmt = (
        sa.select(MeasureCombination)
        .join(Dataset, MeasureCombination.dataset_id == Dataset.id)
        .join(Institution, Dataset.institution_id == Institution.id)
        .order_by(MeasureCombination.id)
    )
    if institution:
        combo_stmt = combo_stmt.where(Institution.code == institution)
    if dataset:
        combo_stmt = combo_stmt.where(Dataset.external_code == dataset)
    if limit:
        combo_stmt = combo_stmt.limit(limit)
    for row in session.scalars(combo_stmt).all():
        before = row.status
        attributes = dict(row.attributes or {})
        raw = (attributes.get("jev") or {}).get("nominal")
        if attributes.get("nominal_method") == "jev" and isinstance(raw, dict):
            score, _confidence = linking.parse_score(raw)
            attributes["nominal_score"] = score
            attributes["nominal_confidence"] = _decisiveness(score)
            row.attributes = attributes
            counts["nominal"] += 1
            if attributes.get("nominal_review"):
                value = nominal_value(score)
                if value is not None:
                    enrich.apply_jev_nominal(
                        row,
                        value=value,
                        score=score,
                        confidence=_decisiveness(score),
                        raw={},
                        method="jev",
                    )
        enrich.refresh_combination_status(row)
        if row.status != before:
            counts["combinations"] += 1

    period_stmt = (
        sa.select(Dataset)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(Dataset.attributes["period_series_jev"].astext.isnot(None))
        .order_by(Dataset.id)
    )
    if institution:
        period_stmt = period_stmt.where(Institution.code == institution)
    if dataset:
        period_stmt = period_stmt.where(Dataset.external_code == dataset)
    if limit:
        period_stmt = period_stmt.limit(limit)
    for member in session.scalars(period_stmt).all():
        attributes = dict(member.attributes or {})
        raw = attributes.get("period_series_jev")
        if not isinstance(raw, dict):
            continue
        score = raw.get("score")
        answer = raw.get("donem")
        if not isinstance(score, (int, float)) and isinstance(answer, dict):
            score, _confidence = linking.parse_score(answer)
        if isinstance(score, (int, float)):
            raw = {**raw, "score": float(score), "confidence": _decisiveness(float(score))}
            attributes["period_series_jev"] = raw
        member.attributes = attributes
        if attributes.get("period_series_review") and isinstance(score, (int, float)):
            decision = period_decision(float(score))
            if decision is not None:
                group_key = (
                    attributes.get("period_series_candidate")
                    or member.donem_serisi_grubu
                    or ""
                )
                enrich.apply_jev_period(
                    member, decision=decision, group_key=group_key, raw=raw
                )
                counts["periods"] += 1

    session.flush()
    return counts


# --------------------------------------------------------------------------- #
# Step 1 - measure dimensions
# --------------------------------------------------------------------------- #


def process_dim(
    session: Session,
    decider: JevDecider,
    tree: ConceptTree,
    item: dict[str, Any],
    refs: dict[str, PromptRef],
) -> str:
    result = decider.decide(
        item["state"], {"olcu": item["question"]}, prompt_ref=refs["measure_dim"]
    )
    answer = _answer_for(result, "olcu")
    score, confidence = _noul_result(answer)
    dimension = session.get(
        DatasetDimension,
        item["dimension_id"],
        options=[selectinload(DatasetDimension.codes)],
    )
    if dimension is None:  # pragma: no cover - raced deletion
        raise RuntimeError(f"dimension {item['dimension_id']} disappeared")
    attributes = dict(dimension.attributes or {})
    attributes["measure_score"] = score
    attributes["measure_confidence"] = confidence
    attributes["measure_jev"] = answer
    decision = dim_decision(score)
    if decision is None:
        dimension.is_measure = None
        dimension.measure_source = None
        attributes["measure_review"] = True
        outcome = "review"
    else:
        dimension.is_measure = decision
        dimension.measure_source = "jev"
        attributes.pop("measure_review", None)
        outcome = "accepted"
    dimension.attributes = attributes
    session.flush()
    return outcome


def _rerun_rules(session: Session, dataset_id: int, tree: ConceptTree, candidates: dict) -> None:
    """Re-enumerate and apply the 2.2a rules for one dataset after a dim decision."""
    dataset, view = _load_view(session, dataset_id)
    plan = rules.compute_dataset_plan(view, tree)
    candidate = candidates.get((view.institution_code, view.external_code))
    enrich.apply_dataset_plan(
        session, dataset, plan, period_candidate=candidate, view=view, tree=tree
    )


# --------------------------------------------------------------------------- #
# Step 2 - type + nature
# --------------------------------------------------------------------------- #


def process_types(
    session: Session,
    decider: JevDecider,
    tree: ConceptTree,
    item: dict[str, Any],
    refs: dict[str, PromptRef],
) -> str:
    result = decider.decide(item["state"], item["questions"], prompt_ref=item["prompt_ref"])
    row = session.get(MeasureCombination, item["combination_id"])
    if row is None:  # pragma: no cover - raced deletion
        raise RuntimeError(f"combination {item['combination_id']} disappeared")
    view = item["view"]

    # Only the fields whose question was asked are written; an already decided
    # field (e.g. rule type, Jev nature) must not be relabelled by this call.
    measure_type: str | None = None
    measure_confidence: float | None = None
    data_nature: str | None = None
    nature_confidence: float | None = None
    raw: dict[str, Any] = {}
    if "tur" in item["questions"]:
        answer = _answer_for(result, "tur")
        measure_type, measure_confidence = _choice_result(answer, tree.measure_types)
        raw["tur"] = answer
    if "nitelik" in item["questions"]:
        answer = _answer_for(result, "nitelik")
        data_nature, nature_confidence = _choice_result(answer, tree.data_natures)
        raw["nitelik"] = answer

    enrich.apply_jev_types(
        view,
        tree,
        row,
        measure_type=measure_type,
        measure_confidence=measure_confidence,
        data_nature=data_nature,
        nature_confidence=nature_confidence,
        raw=raw,
    )
    session.flush()
    return row.status


# --------------------------------------------------------------------------- #
# Step 3 - currency + nominal
# --------------------------------------------------------------------------- #


def process_currency_nominal(
    session: Session,
    decider: JevDecider,
    tree: ConceptTree,
    item: dict[str, Any],
    refs: dict[str, PromptRef],
) -> str:
    result = decider.decide(item["state"], item["questions"], prompt_ref=item["prompt_ref"])
    row = session.get(MeasureCombination, item["combination_id"])
    if row is None:  # pragma: no cover - raced deletion
        raise RuntimeError(f"combination {item['combination_id']} disappeared")
    if "para" in item["questions"]:
        answer = _answer_for(result, "para")
        currency, confidence = _choice_result(answer, CURRENCY_IDS)
        enrich.apply_jev_currency(
            row, currency=currency, confidence=confidence, raw={"para": answer}
        )
    if "nominal" in item["questions"]:
        answer = _answer_for(result, "nominal")
        score, confidence = _noul_result(answer)
        enrich.apply_jev_nominal(
            row,
            value=nominal_value(score),
            score=score,
            confidence=confidence,
            raw={"nominal": answer},
        )
    session.flush()
    return row.status


# --------------------------------------------------------------------------- #
# Step 4 - period series
# --------------------------------------------------------------------------- #


def process_periods(
    session: Session,
    decider: JevDecider,
    tree: ConceptTree,
    item: dict[str, Any],
    refs: dict[str, PromptRef],
) -> str:
    result = decider.decide(
        item["state"], {"donem": item["question"]}, prompt_ref=refs["period_series"]
    )
    answer = _answer_for(result, "donem")
    score, confidence = _noul_result(answer)
    raw = {"donem": answer, "score": score, "confidence": confidence}
    decision = period_decision(score)
    for dataset_id in item["dataset_ids"]:
        dataset = session.get(Dataset, dataset_id)
        if dataset is None:  # pragma: no cover - raced deletion
            continue
        enrich.apply_jev_period(
            dataset, decision=decision, group_key=item["group"], raw=raw
        )
    session.flush()
    return "accepted" if decision is not None else "review"


# --------------------------------------------------------------------------- #
# Progress / pool
# --------------------------------------------------------------------------- #


class _Progress:
    """Thread-safe counters and the periodic progress line."""

    def __init__(self, step: str, total: int, every: int, out: Callable[[str], None]) -> None:
        self.step = step
        self.total = total
        self.every = max(1, every)
        self.out = out
        self.done = 0
        self.accepted = 0
        self.review = 0
        self.errors = 0
        self.aborted = False
        self.error_messages: list[str] = []
        self._start = time.monotonic()
        self._lock = threading.Lock()

    def success(self, outcome: str | None) -> None:
        with self._lock:
            self.done += 1
            if outcome == "review":
                self.review += 1
            else:
                self.accepted += 1
            should_print = self.done % self.every == 0
        if should_print:
            self._print()

    def error(self, item: dict[str, Any], exc: BaseException) -> None:
        message = f"{self._item_id(item)}: {type(exc).__name__}: {exc}"
        with self._lock:
            self.done += 1
            self.errors += 1
            self.error_messages.append(message)
            should_print = self.done % self.every == 0
        logger.error("jev step %s failed for %s", self.step, message, exc_info=exc)
        if should_print:
            self._print()

    @staticmethod
    def _item_id(item: dict[str, Any]) -> str:
        for key in ("combination_id", "dimension_id", "group"):
            if key in item:
                return f"{key}={item[key]}"
        return repr(item)[:80]

    def _print(self) -> None:
        with self._lock:
            done, accepted, review, errors = self.done, self.accepted, self.review, self.errors
            elapsed = max(time.monotonic() - self._start, 1e-6)
        rate = done / elapsed
        self.out(
            f"progress step={self.step} done={done}/{self.total} "
            f"accepted={accepted} review={review} errors={errors} rate={rate:.1f}/s"
        )

    def print_final(self) -> None:
        self._print()


class _ConsecutiveFailures:
    """Count consecutive failures and set the abort event at the limit."""

    def __init__(self, limit: int, abort: threading.Event) -> None:
        self.limit = limit
        self.abort = abort
        self._count = 0
        self._lock = threading.Lock()

    def fail(self) -> None:
        with self._lock:
            self._count += 1
            if self._count >= self.limit:
                self.abort.set()

    def success(self) -> None:
        with self._lock:
            self._count = 0


def _guarded(
    worker: Callable[[dict[str, Any]], str | None],
    item: dict[str, Any],
    progress: _Progress,
    failures: _ConsecutiveFailures,
    abort: threading.Event,
) -> None:
    if abort.is_set():
        return
    try:
        outcome = worker(item)
    except Exception as exc:  # noqa: BLE001 - counted and logged, never silent
        failures.fail()
        progress.error(item, exc)
        return
    failures.success()
    progress.success(outcome)


def run_pool(
    items: list[dict[str, Any]],
    worker: Callable[[dict[str, Any]], str | None],
    *,
    step: str,
    workers: int,
    out: Callable[[str], None] = print,
    every: int = PROGRESS_EVERY,
    failure_limit: int = CONSECUTIVE_FAILURE_LIMIT,
) -> _Progress:
    """Run ``worker`` over ``items`` on a bounded pool (one session per item)."""
    progress = _Progress(step, len(items), every, out)
    if not items:
        progress.print_final()
        return progress
    abort = threading.Event()
    failures = _ConsecutiveFailures(failure_limit, abort)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(_guarded, worker, item, progress, failures, abort) for item in items
        ]
        for future in as_completed(futures):
            future.result()
    progress.aborted = abort.is_set()
    progress.print_final()
    return progress


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #


@dataclass
class JevSummary:
    """Aggregated outcome of one Jev pass."""

    steps: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return sum(step.get("errors", 0) for step in self.steps.values())

    @property
    def aborted(self) -> bool:
        return any(step.get("aborted", 0) for step in self.steps.values())


class JevPass:
    """Runs the Jev steps over the filtered catalog."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        jev_factory: Callable[[], JevDecider] | None = None,
        tree: ConceptTree | None = None,
        workers: int = 4,
        limit: int | None = None,
        institution: str | None = None,
        dataset: str | None = None,
        dry_run: bool = False,
        out: Callable[[str], None] = print,
    ) -> None:
        self.session_factory = session_factory
        self._jev_factory = jev_factory or default_jev_factory
        self.tree = tree or load_tree()
        self.workers = max(1, workers)
        self.limit = limit
        self.institution = institution
        self.dataset = dataset
        self.dry_run = dry_run
        self.out = out
        self._decider: JevDecider | None = None
        self._decider_lock = threading.Lock()
        self._refs: dict[str, PromptRef] = {}
        self._bodies: dict[str, str] = {}

    def _decider_for(self) -> JevDecider:
        if self._decider is None:
            with self._decider_lock:
                if self._decider is None:
                    self._decider = self._jev_factory()
        return self._decider

    def run(self, step: str = "all") -> JevSummary:
        summary = JevSummary()
        with self.session_factory() as session:
            ensure_default_prompts(session)
            session.commit()
            for name, key in PROMPT_KEYS.items():
                view = get_active(session, key)
                self._refs[name] = PromptRef(
                    key=view.key, version=view.version, checksum=view.checksum
                )
                self._bodies[name] = view.body
        steps = _STEPS if step == "all" else (step,)
        for name in steps:
            summary.steps[name] = self._run_step(name)
        return summary

    def _run_step(self, step: str) -> dict[str, int]:
        if self.dry_run:
            return self._dry_run(step)
        if step == "dims":
            return self._run_dims()
        if step == "types":
            return self._run_types()
        if step == "currency":
            return self._run_currency()
        if step == "periods":
            return self._run_periods()
        raise ValueError(f"unknown step {step!r}")

    def _dry_run(self, step: str) -> dict[str, int]:
        with self.session_factory() as session:
            items = self._collect(step, session)
        for item in items[: self.limit] if self.limit else items:
            self.out(json.dumps(self._preview(step, item), ensure_ascii=False, default=str))
        self.out(f"dry-run step={step} items={len(items[: self.limit] if self.limit else items)}")
        return {"processed": 0, "accepted": 0, "review": 0, "errors": 0, "aborted": 0}

    @staticmethod
    def _preview(step: str, item: dict[str, Any]) -> dict[str, Any]:
        preview = {"step": step, "state": item.get("state")}
        if "question" in item:
            preview["questions"] = {"q": item["question"]}
        if "questions" in item:
            preview["questions"] = item["questions"]
        return preview

    def _worker(self, step: str) -> Callable[[dict[str, Any]], str | None]:
        function = {
            "dims": process_dim,
            "types": process_types,
            "currency": process_currency_nominal,
            "periods": process_periods,
        }[step]

        def work(item: dict[str, Any]) -> str | None:
            decider = self._decider_for()
            with self.session_factory() as session:
                outcome = function(session, decider, self.tree, item, self._refs)
                session.commit()
                return outcome

        return work

    def _report(self, step: str, progress: _Progress) -> dict[str, int]:
        return {
            "processed": progress.done,
            "accepted": progress.accepted,
            "review": progress.review,
            "errors": progress.errors,
            "aborted": 1 if progress.aborted else 0,
        }

    def _collect(self, step: str, session: Session) -> list[dict[str, Any]]:
        if step == "dims":
            return self._collect_dims(session)
        if step == "types":
            return self._collect_types(session)
        if step == "currency":
            return self._collect_currency(session)
        if step == "periods":
            return self._collect_periods(session)
        raise ValueError(step)

    # -- filters -----------------------------------------------------------

    def _filter_dataset(self, statement: sa.Select[Any]) -> sa.Select[Any]:
        if self.dataset:
            statement = statement.where(Dataset.external_code == self.dataset)
        if self.institution:
            statement = statement.where(Institution.code == self.institution)
        if self.limit:
            statement = statement.limit(self.limit)
        return statement

    def _collect_dims(self, session: Session) -> list[dict[str, Any]]:
        stmt = (
            sa.select(DatasetDimension, Dataset, Institution.code)
            .join(Dataset, DatasetDimension.dataset_id == Dataset.id)
            .join(Institution, Dataset.institution_id == Institution.id)
            .where(DatasetDimension.is_measure.is_(None))
            .where(
                sa.or_(
                    DatasetDimension.measure_source.is_(None),
                    DatasetDimension.measure_source == "rule",
                )
            )
            .options(selectinload(DatasetDimension.codes))
            .order_by(DatasetDimension.id)
        )
        stmt = self._filter_dataset(stmt)
        items: list[dict[str, Any]] = []
        for dimension, dataset, institution_code in session.execute(stmt).all():
            if dimension.measure_source == "manual":  # defensive: never re-ask
                continue
            items.append(
                {
                    "dimension_id": dimension.id,
                    "dataset_id": dataset.id,
                    "state": build_dim_state(dataset, institution_code, dimension),
                    "question": dim_question(self._bodies["measure_dim"]),
                }
            )
        return items

    def _collect_types(self, session: Session) -> list[dict[str, Any]]:
        stmt = (
            sa.select(MeasureCombination, Dataset, Institution.code, Institution.name)
            .join(Dataset, MeasureCombination.dataset_id == Dataset.id)
            .join(Institution, Dataset.institution_id == Institution.id)
            .where(
                sa.or_(
                    MeasureCombination.measure_type.is_(None),
                    MeasureCombination.data_nature.is_(None),
                )
            )
            .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
            .order_by(MeasureCombination.id)
        )
        stmt = self._filter_dataset(stmt)
        items: list[dict[str, Any]] = []
        views = _ViewCache()
        for combination, dataset, institution_code, institution_name in session.execute(stmt).all():
            view = views.get(dataset, institution_code, institution_name)
            questions = questions_for_combination(combination, self.tree, self._bodies)
            prompt_ref = self._refs["measure_type" if "tur" in questions else "data_nature"]
            items.append(
                {
                    "combination_id": combination.id,
                    "dataset_id": dataset.id,
                    "view": view,
                    "state": build_type_state(dataset, institution_code, view, combination),
                    "questions": questions,
                    "prompt_ref": prompt_ref,
                    "measure_type": combination.measure_type,
                    "type_confidence": combination.type_confidence,
                    "data_nature": combination.data_nature,
                    "nature_confidence": combination.nature_confidence,
                }
            )
        return items

    def _collect_currency(self, session: Session) -> list[dict[str, Any]]:
        currency_pending = MeasureCombination.attributes["currency_pending"].astext == "true"
        nominal_pending = MeasureCombination.attributes["nominal_pending"].astext == "true"
        stmt = (
            sa.select(MeasureCombination, Dataset, Institution.code, Institution.name)
            .join(Dataset, MeasureCombination.dataset_id == Dataset.id)
            .join(Institution, Dataset.institution_id == Institution.id)
            .where(sa.or_(currency_pending, nominal_pending))
            .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
            .order_by(MeasureCombination.id)
        )
        stmt = self._filter_dataset(stmt)
        items: list[dict[str, Any]] = []
        views = _ViewCache()
        for combination, dataset, institution_code, institution_name in session.execute(stmt).all():
            view = views.get(dataset, institution_code, institution_name)
            questions = questions_for_currency(combination, self._bodies)
            prompt_ref = self._refs["currency" if "para" in questions else "nominal"]
            items.append(
                {
                    "combination_id": combination.id,
                    "dataset_id": dataset.id,
                    "view": view,
                    "state": build_currency_state(
                        dataset, institution_code, view, combination
                    ),
                    "questions": questions,
                    "prompt_ref": prompt_ref,
                }
            )
        return items

    def _collect_periods(self, session: Session) -> list[dict[str, Any]]:
        rows = enrich.period_candidate_rows(session)
        if self.dataset:
            rows = [row for row in rows if row[0] == self.dataset]
        if self.limit:
            rows = rows[: self.limit]
        groups: dict[str, list[str]] = {}
        for code, group in rows:
            groups.setdefault(group, []).append(code)
        items: list[dict[str, Any]] = []
        for group, _codes in groups.items():
            members = session.scalars(
                sa.select(Dataset)
                .where(Dataset.attributes["period_series_candidate"].astext == group)
                .order_by(Dataset.id)
            ).all()
            if self.institution:
                members = [
                    dataset
                    for dataset in members
                    if session.get(Institution, dataset.institution_id).code == self.institution
                ]
            if _period_group_decided(members, group):
                continue
            items.append(
                {
                    "group": group,
                    "dataset_ids": [dataset.id for dataset in members],
                    "state": build_period_state(members, group),
                    "question": period_question(self._bodies["period_series"]),
                }
            )
        return items

    # -- runners -----------------------------------------------------------

    def _run_dims(self) -> dict[str, int]:
        with self.session_factory() as session:
            items = self._collect_dims(session)
        progress = run_pool(
            items, self._worker("dims"), step="dims", workers=self.workers, out=self.out
        )
        rerun_errors = 0
        affected = sorted({item["dataset_id"] for item in items})
        if affected:
            with self.session_factory() as session:
                candidates = rules.period_series_candidates(enrich.load_all_dataset_names(session))
                for dataset_id in affected:
                    try:
                        _rerun_rules(session, dataset_id, self.tree, candidates)
                        session.commit()
                    except Exception as exc:  # noqa: BLE001 - counted, not silent
                        session.rollback()
                        rerun_errors += 1
                        logger.exception("enrich re-run failed for dataset %s", dataset_id)
                        self.out(
                            f"error step=dims dataset={dataset_id} "
                            f"{type(exc).__name__}: {exc}"
                        )
        result = self._report("dims", progress)
        result["errors"] += rerun_errors
        return result

    def _run_types(self) -> dict[str, int]:
        with self.session_factory() as session:
            items = self._collect_types(session)
        progress = run_pool(
            items, self._worker("types"), step="types", workers=self.workers, out=self.out
        )
        errors = recompute_touched_flags(
            self.session_factory,
            (item["dataset_id"] for item in items),
            step="types",
            out=self.out,
        )
        result = self._report("types", progress)
        result["errors"] += errors
        return result

    def _run_currency(self) -> dict[str, int]:
        with self.session_factory() as session:
            items = self._collect_currency(session)
        progress = run_pool(
            items, self._worker("currency"), step="currency", workers=self.workers, out=self.out
        )
        errors = recompute_touched_flags(
            self.session_factory,
            (item["dataset_id"] for item in items),
            step="currency",
            out=self.out,
        )
        result = self._report("currency", progress)
        result["errors"] += errors
        return result

    def _run_periods(self) -> dict[str, int]:
        with self.session_factory() as session:
            items = self._collect_periods(session)
        progress = run_pool(
            items, self._worker("periods"), step="periods", workers=self.workers, out=self.out
        )
        return self._report("periods", progress)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

_CONFIDENCE_BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0001))


def _bucket(confidence: float) -> str:
    for low, high in _CONFIDENCE_BUCKETS:
        if low <= confidence < high:
            return f"{low:.1f}-{min(high, 1.0):.1f}"
    return "0.9-1.0"


def report_lines(session: Session) -> list[str]:
    """Counts per step plus a confidence histogram, read from the database."""
    lines: list[str] = []
    dim_total = session.scalar(sa.select(sa.func.count()).select_from(DatasetDimension)) or 0
    dim_sources: Counter = Counter()
    dim_confidences: list[float] = []
    for is_measure, source, count in session.execute(
        sa.select(
            DatasetDimension.is_measure,
            DatasetDimension.measure_source,
            sa.func.count(),
        ).group_by(DatasetDimension.is_measure, DatasetDimension.measure_source)
    ):
        key = "pending" if is_measure is None and source is None else (source or "unset")
        dim_sources[key] += count
    for attributes in session.scalars(sa.select(DatasetDimension.attributes)).all():
        confidence = (attributes or {}).get("measure_confidence")
        if isinstance(confidence, (int, float)):
            dim_confidences.append(float(confidence))
    lines.append(
        f"dims: total={dim_total} rule={dim_sources.get('rule', 0)} "
        f"jev={dim_sources.get('jev', 0)} manual={dim_sources.get('manual', 0)} "
        f"pending={dim_sources.get('pending', 0)} review={dim_sources.get('unset', 0)}"
    )

    combo_total = (
        session.scalar(sa.select(sa.func.count()).select_from(MeasureCombination)) or 0
    )
    status_counts: Counter = Counter()
    type_methods: Counter = Counter()
    for status, count in session.execute(
        sa.select(MeasureCombination.status, sa.func.count()).group_by(
            MeasureCombination.status
        )
    ):
        status_counts[status] += count
    for method, count in session.execute(
        sa.select(MeasureCombination.type_method, sa.func.count()).group_by(
            MeasureCombination.type_method
        )
    ):
        type_methods[method] += count
    lines.append(
        f"types: total={combo_total} rule={type_methods.get('rule', 0)} "
        f"jev={type_methods.get('jev', 0)} manual={type_methods.get('manual', 0)} "
        f"pending={type_methods.get(None, 0)}"
    )
    lines.append(
        f"status: accepted={status_counts.get('accepted', 0)} "
        f"review={status_counts.get('review', 0)} pending={status_counts.get('pending', 0)}"
    )

    confidences: list[float] = list(dim_confidences)
    for row in session.scalars(sa.select(MeasureCombination)).all():
        for value in (
            row.type_confidence if row.type_method == "jev" else None,
            row.nature_confidence if row.nature_method == "jev" else None,
        ):
            if isinstance(value, (int, float)):
                confidences.append(float(value))
        attributes = row.attributes or {}
        for method_key, confidence_key in (
            ("currency_method", "currency_confidence"),
            ("nominal_method", "nominal_confidence"),
        ):
            if attributes.get(method_key) == "jev" and isinstance(
                attributes.get(confidence_key), (int, float)
            ):
                confidences.append(float(attributes[confidence_key]))
    histogram: Counter = Counter()
    for confidence in confidences:
        histogram[_bucket(confidence)] += 1
    bucket_text = ", ".join(f"{name}={histogram.get(name, 0)}" for name, _ in
                            ((f"{low:.1f}-{min(high, 1.0):.1f}", (low, high))
                             for low, high in _CONFIDENCE_BUCKETS))
    lines.append(f"jev confidence histogram: {bucket_text}")
    return lines


# --------------------------------------------------------------------------- #
# Review list
# --------------------------------------------------------------------------- #


def _top_probabilities(answer: Any, limit: int = 2) -> str:
    if not isinstance(answer, dict):
        return ""
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        return ""
    pairs = []
    for key, value in probabilities.items():
        try:
            pairs.append((float(value), str(key)))
        except (TypeError, ValueError):
            continue
    pairs.sort(reverse=True)
    return "; ".join(f"{key}={value:.2f}" for value, key in pairs[:limit])


def review_rows(session: Session, *, kind: str, limit: int | None = None) -> list[list[str]]:
    """TSV rows for the review list of one field kind."""
    rows: list[list[str]] = []
    if kind == "dims":
        statement = (
            sa.select(DatasetDimension, Dataset.external_code)
            .join(Dataset, DatasetDimension.dataset_id == Dataset.id)
            .where(DatasetDimension.is_measure.is_(None))
            .order_by(DatasetDimension.id)
        )
        if limit:
            statement = statement.limit(limit)
        for dimension, dataset_code in session.execute(statement).all():
            attributes = dimension.attributes or {}
            rows.append(
                [
                    str(dimension.id),
                    dataset_code,
                    dimension.label,
                    "",
                    "?",
                    f"{attributes.get('measure_confidence', '')}",
                    "",
                ]
            )
    elif kind == "types":
        statement = (
            sa.select(MeasureCombination, Dataset.external_code)
            .join(Dataset, MeasureCombination.dataset_id == Dataset.id)
            .where(MeasureCombination.status == "review")
            .order_by(MeasureCombination.id)
        )
        if limit:
            statement = statement.limit(limit)
        for combination, dataset_code in session.execute(statement).all():
            raw = (combination.attributes or {}).get("jev") or {}
            rows.append(
                [
                    str(combination.id),
                    dataset_code,
                    combination.label,
                    "",
                    combination.measure_type or "?",
                    (
                        f"{combination.type_confidence}"
                        if combination.type_confidence is not None
                        else ""
                    ),
                    _top_probabilities(raw.get("tur")),
                ]
            )
    elif kind in ("currency", "nominal"):
        key = "currency_pending" if kind == "currency" else "nominal_pending"
        method_key = f"{kind}_method"
        confidence_key = f"{kind}_confidence"
        statement = (
            sa.select(MeasureCombination, Dataset.external_code)
            .join(Dataset, MeasureCombination.dataset_id == Dataset.id)
            .where(
                sa.or_(
                    MeasureCombination.attributes[key].astext == "true",
                    MeasureCombination.attributes[method_key].astext == "jev",
                    MeasureCombination.attributes[f"{kind}_review"].astext == "true",
                )
            )
            .order_by(MeasureCombination.id)
        )
        if limit:
            statement = statement.limit(limit)
        for combination, dataset_code in session.execute(statement).all():
            attributes = combination.attributes or {}
            raw = (attributes.get("jev") or {}).get("para" if kind == "currency" else "nominal")
            proposed = (
                combination.para_birimi if kind == "currency" else combination.nominal_mi
            )
            rows.append(
                [
                    str(combination.id),
                    dataset_code,
                    combination.label,
                    "",
                    proposed or attributes.get(kind) or "?",
                    f"{attributes.get(confidence_key, '')}",
                    _top_probabilities(raw),
                ]
            )
    elif kind == "periods":
        statement = (
            sa.select(Dataset)
            .where(Dataset.attributes["period_series_review"].astext == "true")
            .order_by(Dataset.id)
        )
        if limit:
            statement = statement.limit(limit)
        for dataset in session.scalars(statement).all():
            attributes = dataset.attributes or {}
            rows.append(
                [
                    dataset.external_code,
                    attributes.get("period_series_candidate", ""),
                    dataset.name,
                    "",
                    "?",
                    "",
                    "",
                ]
            )
    return rows


# --------------------------------------------------------------------------- #
# Manual review setters
# --------------------------------------------------------------------------- #


def _validate_choice(value: str, allowed: Iterable[str], label: str) -> str:
    if value not in set(allowed):
        raise ValueError(f"{label} must be one of {sorted(set(allowed))}, got {value!r}")
    return value


def set_combination(
    session: Session,
    combination_id: int,
    *,
    measure_type: str | None = None,
    data_nature: str | None = None,
    currency: str | None = None,
    nominal: str | None = None,
) -> MeasureCombination:
    """Manual override of one combination (method ``manual``, marked reviewed)."""
    tree = load_tree()
    row = session.get(MeasureCombination, combination_id)
    if row is None:
        raise ValueError(f"combination {combination_id} not found")
    if measure_type is not None:
        _validate_choice(measure_type, tree.measure_types, "measure_type")
    if data_nature is not None:
        _validate_choice(data_nature, tree.data_natures, "data_nature")
    if currency is not None:
        _validate_choice(currency, CURRENCY_IDS, "currency")
    if nominal is not None:
        _validate_choice(nominal, ("reel", "nominal"), "nominal")

    dataset, view = _load_view(session, row.dataset_id)
    if measure_type is not None or data_nature is not None:
        if measure_type is not None:
            row.measure_type = measure_type
            row.type_method = "manual"
            row.type_confidence = 1.0
        if data_nature is not None:
            row.data_nature = data_nature
            row.nature_method = "manual"
            row.nature_confidence = 1.0
        plan = enrich.plan_for_known_type(
            view,
            tree,
            row,
            measure_type=row.measure_type,
            data_nature=row.data_nature,
            measure_method=row.type_method,
            nature_method=row.nature_method,
            measure_confidence=row.type_confidence,
            nature_confidence=row.nature_confidence,
        )
        row.source_aggregation = plan.source_aggregation
        row.aggregation = plan.aggregation
        row.aggregation_conflict = plan.aggregation_conflict
        row.mevsim_arindirilmis = plan.mevsim_arindirilmis
        row.kumulatif = plan.kumulatif
        attributes = dict(row.attributes or {})
        enrich._merge_currency(row, plan, attributes)
        enrich._merge_nominal(row, plan, attributes)
        row.attributes = attributes
    if currency is not None:
        enrich.apply_jev_currency(
            row, currency=currency, confidence=1.0, raw={}, method="manual"
        )
    if nominal is not None:
        enrich.apply_jev_nominal(
            row, value=nominal, confidence=1.0, raw={}, method="manual"
        )
    attributes = dict(row.attributes or {})
    attributes["reviewed"] = True
    row.attributes = attributes
    row.status = "accepted"
    session.flush()
    enrich.recompute_dataset_flags(session, dataset)
    return row


def set_dimension(session: Session, dimension_id: int, *, measure: bool) -> DatasetDimension:
    """Manual override of one dimension's ``is_measure``; re-enumerate its dataset."""
    dimension = session.get(DatasetDimension, dimension_id)
    if dimension is None:
        raise ValueError(f"dimension {dimension_id} not found")
    attributes = dict(dimension.attributes or {})
    attributes.pop("measure_review", None)
    dimension.attributes = attributes
    dimension.is_measure = bool(measure)
    dimension.measure_source = "manual"
    session.flush()
    dataset_id = dimension.dataset_id
    candidates = rules.period_series_candidates(enrich.load_all_dataset_names(session))
    _rerun_rules(session, dataset_id, load_tree(), candidates)
    return dimension


def set_period(session: Session, group_key: str, *, accepted: bool) -> int:
    """Manually confirm/reject a period-series candidate group; return members touched."""
    datasets = session.scalars(
        sa.select(Dataset).where(
            Dataset.attributes["period_series_candidate"].astext == group_key
        )
    ).all()
    for dataset in datasets:
        enrich.apply_jev_period(
            dataset,
            decision="yes" if accepted else "no",
            group_key=group_key,
            raw={},
        )
        attributes = dict(dataset.attributes or {})
        attributes["period_series_manual"] = True
        attributes["period_series_reviewed"] = True
        attributes["period_series_method"] = "manual"
        # A manual decision settles the group: never ask it again.
        attributes.pop("period_series_review", None)
        attributes.pop("period_series_candidate", None)
        dataset.attributes = attributes
    session.flush()
    return len(datasets)
