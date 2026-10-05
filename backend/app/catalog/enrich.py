"""CLI: ``python -m app.catalog.enrich`` (Task 2.2a).

Subcommands:

- ``rules [--dataset CODE] [--institution tuik|tcmb|hmb] [--dry-run]`` — run the
  deterministic rules (description, measure dimensions, combinations, aggregation,
  currency/nominal/seasonal, dataset flags) and persist them. Writes commit per
  dataset, so one bad dataset never loses the others; a failing dataset is
  reported and makes the exit code 1.
- ``report`` — the same summary read back from the database.

Only the rule pass runs here; Task 2.2b adds the Jev pass on the rows this
creates.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.catalog import enrich_rules as rules
from app.catalog.tree import ConceptTree, load_tree
from app.data.models import (
    Dataset,
    DatasetDimension,
    Institution,
    MeasureCombination,
)

logger = logging.getLogger("app.catalog.enrich")

_REVIEWED = "reviewed"

#: Jev auto-accept / auto-reject confidence thresholds (user decision 2.2b).
#: A yes/choice at or above ``ACCEPT_CONFIDENCE`` is accepted; at or below
#: ``REJECT_CONFIDENCE`` it is a definite no; between them it goes to review.
ACCEPT_CONFIDENCE = 0.60
REJECT_CONFIDENCE = 0.40


# --------------------------------------------------------------------------- #
# DB -> view
# --------------------------------------------------------------------------- #


def _build_view(
    dataset: Dataset,
    institution_code: str,
    institution_name: str,
) -> rules.DatasetView:
    dimensions = tuple(
        rules.DimensionView(
            code=dimension.code,
            label=dimension.label,
            role=dimension.role,
            position=dimension.position,
            attributes=dict(dimension.attributes or {}),
            codes=tuple(
                rules.CodeView(
                    code=code.code,
                    label=code.label,
                    attributes=dict(code.attributes or {}),
                )
                for code in dimension.codes
            ),
            is_measure=dimension.is_measure,
            measure_source=dimension.measure_source,
        )
        for dimension in dataset.dimensions
    )
    return rules.DatasetView(
        institution_code=institution_code,
        institution_name=institution_name,
        external_code=dataset.external_code,
        name=dataset.name,
        description=dataset.description,
        source_category=dataset.source_category,
        attributes=dict(dataset.attributes or {}),
        dimensions=dimensions,
    )


def load_views(
    session: Session,
    *,
    dataset_code: str | None = None,
    institution: str | None = None,
) -> list[rules.DatasetView]:
    """Load dataset views (with dimensions/codes) matching the filters."""
    statement = (
        sa.select(Dataset, Institution.code, Institution.name)
        .join(Institution, Dataset.institution_id == Institution.id)
        .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
        .order_by(Dataset.id)
    )
    if dataset_code:
        statement = statement.where(Dataset.external_code == dataset_code)
    if institution:
        statement = statement.where(Institution.code == institution)
    rows = session.execute(statement).all()
    return [
        _build_view(dataset, institution_code, institution_name)
        for dataset, institution_code, institution_name in rows
    ]


def load_all_dataset_names(session: Session) -> list[tuple[str, str, str]]:
    """``(institution_code, external_code, name)`` for every dataset (grouping)."""
    rows = session.execute(
        sa.select(Institution.code, Dataset.external_code, Dataset.name).join(
            Institution, Dataset.institution_id == Institution.id
        )
    ).all()
    return [(code, external_code, name) for code, external_code, name in rows]


# --------------------------------------------------------------------------- #
# plan -> DB
# --------------------------------------------------------------------------- #


def _combination_key(codes: dict[str, str]) -> str:
    return json.dumps(codes, sort_keys=True, ensure_ascii=False)


#: Attributes keys a rule may own on a combination (companion method keys).
_CURRENCY_METHOD = "currency_method"
_NOMINAL_METHOD = "nominal_method"


def _row_locked(row: MeasureCombination) -> bool:
    """True when a human reviewed the row: never touched by a rule or Jev.

    ``status == "accepted"`` alone is NOT a lock: a row accepted only because
    Jev answered it must still be updatable, because rules win over Jev.
    """
    return (row.attributes or {}).get(_REVIEWED) is True


def _is_owned(row: MeasureCombination) -> bool:
    """True when a rule run must not delete a stale row.

    Only a human review marker or a manual value protects a row. A Jev/rule
    answer (or ``status == "accepted"``) does NOT: the row is stale and must go
    so the new enumeration is the only shape left.
    """
    if _row_locked(row):
        return True
    if row.type_method == "manual" or row.nature_method == "manual":
        return True
    attributes = row.attributes or {}
    return (
        attributes.get(_CURRENCY_METHOD) == "manual" or attributes.get(_NOMINAL_METHOD) == "manual"
    )


def _rule_can_write(value: object, method: object) -> bool:
    """A rule writes a field only when it is empty or the rule set it itself."""
    return value is None or method == "rule"


def _rule_overwrites(method: object, plan_decides: bool) -> bool:
    """Whether the rule pass may write a field, given who set it.

    - ``manual``: never (a human owns it).
    - ``jev``: only when the rule actually decides the field; a rule ``None``
      must not erase the Jev answer (rules win, but only when they speak).
    - empty / ``rule``: the rule fully owns the field and may reset it to NULL.
    """
    if method == "manual":
        return False
    if method == "jev":
        return plan_decides
    return True


def _set_or_clear(attributes: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        attributes.pop(key, None)
    else:
        attributes[key] = value


def _merge_currency(
    row: MeasureCombination,
    plan: rules.CombinationPlan,
    attributes: dict[str, Any],
) -> None:
    """A rule fully controls a field it owns (possibly resetting it to pending).

    A Jev/manual value is never touched. A field whose current method is
    ``rule`` is replaced by the new rule result even when the new result is
    "no decision" (reset to ``NULL`` + pending), so an old wrong value cannot
    survive a rules change.
    """
    plan_decides = plan.para_birimi is not None or plan.attributes.get("currency") == "n/a"
    if not _rule_overwrites(attributes.get(_CURRENCY_METHOD), plan_decides):
        return
    if plan.para_birimi is not None:
        value = rules.normalize_currency(plan.para_birimi)
        row.para_birimi = value
        attributes["currency"] = value
        attributes[_CURRENCY_METHOD] = "rule"
        attributes["currency_confidence"] = 1.0
        attributes.pop("currency_pending", None)
    elif plan.attributes.get("currency") == "n/a":
        row.para_birimi = None
        attributes["currency"] = "n/a"
        attributes[_CURRENCY_METHOD] = "rule"
        attributes["currency_confidence"] = 1.0
        attributes.pop("currency_pending", None)
    else:
        row.para_birimi = None
        attributes.pop("currency", None)
        attributes.pop(_CURRENCY_METHOD, None)
        attributes.pop("currency_confidence", None)
        attributes["currency_pending"] = True


def _merge_nominal(
    row: MeasureCombination,
    plan: rules.CombinationPlan,
    attributes: dict[str, Any],
) -> None:
    plan_decides = plan.nominal_mi is not None or plan.attributes.get("nominal") == "n/a"
    if not _rule_overwrites(attributes.get(_NOMINAL_METHOD), plan_decides):
        return
    if plan.nominal_mi is not None:
        row.nominal_mi = plan.nominal_mi
        attributes["nominal"] = plan.attributes.get("nominal")
        attributes[_NOMINAL_METHOD] = "rule"
        attributes["nominal_confidence"] = 1.0
        attributes.pop("nominal_pending", None)
    elif plan.attributes.get("nominal") == "n/a":
        row.nominal_mi = None
        attributes["nominal"] = "n/a"
        attributes[_NOMINAL_METHOD] = "rule"
        attributes["nominal_confidence"] = 1.0
        attributes.pop("nominal_pending", None)
    else:
        row.nominal_mi = None
        attributes.pop("nominal", None)
        attributes.pop(_NOMINAL_METHOD, None)
        attributes.pop("nominal_confidence", None)
        attributes["nominal_pending"] = True


def _merge_plan_into_row(row: MeasureCombination, plan: rules.CombinationPlan) -> None:
    """Field-level merge: a deciding rule wins over Jev; manual/reviewed is safe.

    A rule value overwrites a ``jev`` value (the raw answer stays in
    ``attributes.jev`` for audit) and a ``rule`` value may be reset to ``NULL``
    when the new rules no longer decide it. A rule that does NOT decide leaves
    the Jev value in place, and a ``manual`` value or reviewed row is never
    touched.
    """
    if _row_locked(row):
        return
    row.codes = plan.codes
    row.label = plan.label
    # source aggregation is source data: always refreshed.
    row.source_aggregation = plan.source_aggregation

    attributes = dict(row.attributes or {})
    if _rule_overwrites(row.type_method, plan.measure_type is not None):
        row.measure_type = plan.measure_type
        row.type_method = plan.type_method
        row.type_confidence = plan.type_confidence
        row.aggregation = plan.aggregation
        row.aggregation_conflict = plan.aggregation_conflict
        _set_or_clear(attributes, "type_rule", plan.attributes.get("type_rule"))
        _set_or_clear(attributes, "type_rule_tier", plan.attributes.get("type_rule_tier"))

    if _rule_overwrites(row.nature_method, plan.data_nature is not None):
        row.data_nature = plan.data_nature
        row.nature_method = plan.nature_method
        row.nature_confidence = plan.nature_confidence
        _set_or_clear(attributes, "nature_rule", plan.attributes.get("nature_rule"))

    # Seasonal and cumulative are rule-only fields: always recomputed.
    row.mevsim_arindirilmis = plan.mevsim_arindirilmis
    row.kumulatif = plan.kumulatif
    _merge_currency(row, plan, attributes)
    _merge_nominal(row, plan, attributes)
    row.attributes = attributes
    refresh_combination_status(row)


def combination_status(row: MeasureCombination) -> str:
    """``accepted`` when every applicable field is decided with conf >= 0.70.

    ``review`` when a field is undecided after processing or a Jev confidence is
    below the threshold; ``pending`` only while nothing has been processed.
    Rule/manual values count as confidence ``1.0``.
    """
    if _row_locked(row):
        return "accepted"
    attributes = row.attributes or {}
    confidences: list[float] = []
    undecided = False

    if row.measure_type is not None:
        confidences.append(_decision_confidence(row.type_confidence, row.type_method))
    else:
        undecided = True
    if row.data_nature is not None:
        confidences.append(_decision_confidence(row.nature_confidence, row.nature_method))
    else:
        undecided = True
    if row.measure_type in rules.MONETARY_TYPES:
        if row.para_birimi is not None or attributes.get("currency") == "n/a":
            confidences.append(
                _decision_confidence(
                    attributes.get("currency_confidence"), attributes.get(_CURRENCY_METHOD)
                )
            )
        else:
            undecided = True
    if row.measure_type in {"akim_tutar", "stok_tutar", "kisi_basina"}:
        if row.nominal_mi is not None or attributes.get("nominal") == "n/a":
            confidences.append(
                _decision_confidence(
                    attributes.get("nominal_confidence"), attributes.get(_NOMINAL_METHOD)
                )
            )
        else:
            undecided = True

    # "Processed" means a Jev/manual answer or an explicit review marker exists;
    # a rule-only row still awaiting Jev stays ``pending``.
    processed = (
        bool(attributes.get("jev"))
        or row.type_method == "jev"
        or row.nature_method == "jev"
        or attributes.get(_CURRENCY_METHOD) == "jev"
        or attributes.get(_NOMINAL_METHOD) == "jev"
        or attributes.get("nominal_review") is True
    )
    if undecided:
        return "review" if processed else "pending"
    if all(confidence >= ACCEPT_CONFIDENCE for confidence in confidences):
        return "accepted"
    return "review"


def _decision_confidence(confidence: object, method: object) -> float:
    if method in (None, "rule", "manual"):
        return 1.0
    try:
        return float(confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def refresh_combination_status(row: MeasureCombination) -> None:
    """Set ``row.status`` for an unlocked row (accepted / review / pending)."""
    if _row_locked(row):
        return
    row.status = combination_status(row)


def apply_jev_currency(
    row: MeasureCombination,
    *,
    currency: str | None,
    confidence: float | None,
    raw: dict[str, Any],
    method: str = "jev",
) -> None:
    """Write a currency answer (``yok`` -> ``n/a``)."""
    if _row_locked(row):
        return
    attributes = dict(row.attributes or {})
    # Jev fills only an unanswered field; the manual setter overrides anything.
    if currency is not None and (method == "manual" or attributes.get(_CURRENCY_METHOD) is None):
        if currency == "yok":
            row.para_birimi = None
            attributes["currency"] = "n/a"
        else:
            value = rules.normalize_currency(currency)
            row.para_birimi = value
            attributes["currency"] = value
        attributes[_CURRENCY_METHOD] = method
        attributes["currency_confidence"] = confidence
        attributes.pop("currency_pending", None)
    jev_raw = dict(attributes.get("jev") or {})
    jev_raw.update(raw)
    attributes["jev"] = jev_raw
    row.attributes = attributes
    refresh_combination_status(row)


def apply_jev_nominal(
    row: MeasureCombination,
    *,
    value: str | None,
    score: float | None = None,
    confidence: float | None,
    raw: dict[str, Any],
    method: str = "jev",
) -> None:
    """Write a nominal/reel answer; the ambiguous middle goes to review.

    ``confidence`` is the decisiveness (``max(p, 1-p)`` for a noul answer) and
    ``score`` the raw yes-probability, kept for a later ``refresh-status``.
    """
    if _row_locked(row):
        return
    attributes = dict(row.attributes or {})
    if value in ("reel", "nominal") and (
        method == "manual" or attributes.get(_NOMINAL_METHOD) is None
    ):
        row.nominal_mi = value
        attributes["nominal"] = value
        attributes[_NOMINAL_METHOD] = method
        attributes["nominal_confidence"] = confidence
        if score is not None:
            attributes["nominal_score"] = score
        attributes.pop("nominal_pending", None)
    elif value is None:
        attributes["nominal_review"] = True
        attributes.pop("nominal_pending", None)
    jev_raw = dict(attributes.get("jev") or {})
    jev_raw.update(raw)
    attributes["jev"] = jev_raw
    row.attributes = attributes
    refresh_combination_status(row)


def apply_jev_period(
    dataset: Dataset,
    *,
    decision: str | None,
    group_key: str,
    raw: dict[str, Any],
) -> None:
    """Write a Jev period-series answer on one member dataset."""
    attributes = dict(dataset.attributes or {})
    if decision == "yes":
        dataset.donem_serisi = True
        dataset.donem_serisi_grubu = group_key
        attributes.pop("period_series_candidate", None)
        attributes.pop("period_series_review", None)
        attributes.pop("period_series_rejected", None)
    elif decision == "no":
        dataset.donem_serisi = False
        dataset.donem_serisi_grubu = None
        attributes.pop("period_series_candidate", None)
        attributes.pop("period_series_review", None)
        attributes["period_series_rejected"] = True
    else:
        attributes["period_series_review"] = True
    attributes["period_series_jev"] = raw
    dataset.attributes = attributes


def code_views_for(view: rules.DatasetView, codes: dict[str, Any]) -> list[rules.CodeView] | None:
    """Resolve a combination's codes to their :class:`CodeView` objects."""
    dimensions = {dimension.code: dimension for dimension in view.dimensions}
    resolved: list[rules.CodeView] = []
    for dimension_code, code_value in codes.items():
        dimension = dimensions.get(dimension_code)
        if dimension is None:
            return None
        matched = next((code for code in dimension.codes if code.code == code_value), None)
        if matched is None:
            return None
        resolved.append(matched)
    return resolved


def plan_for_known_type(
    view: rules.DatasetView,
    tree: ConceptTree,
    row: MeasureCombination,
    *,
    measure_type: str | None,
    data_nature: str | None,
    measure_method: str | None,
    nature_method: str | None,
    measure_confidence: float | None,
    nature_confidence: float | None,
) -> rules.CombinationPlan:
    """Recompute the derived fields for an already-decided type/nature."""
    code_views = code_views_for(view, dict(row.codes or {}))
    return rules.derived_combination(
        view,
        tree,
        dict(row.codes or {}),
        row.label,
        code_views or [],
        measure_type=measure_type,
        data_nature=data_nature,
        type_method=measure_method,
        nature_method=nature_method,
        type_confidence=measure_confidence,
        nature_confidence=nature_confidence,
    )


def apply_jev_types(
    view: rules.DatasetView,
    tree: ConceptTree,
    row: MeasureCombination,
    *,
    measure_type: str | None,
    measure_confidence: float | None,
    data_nature: str | None,
    nature_confidence: float | None,
    raw: dict[str, Any],
    method: str = "jev",
) -> None:
    """Write a type/nature answer (Jev or manual) and recompute derived fields."""
    if _row_locked(row):
        return
    # Jev never overwrites a rule value (rules win); it only fills an unanswered
    # field. The manual setter passes the value directly elsewhere.
    if measure_type is not None and row.measure_type is None:
        row.measure_type = measure_type
        row.type_method = method
        row.type_confidence = measure_confidence
    if data_nature is not None and row.data_nature is None:
        row.data_nature = data_nature
        row.nature_method = method
        row.nature_confidence = nature_confidence

    plan = plan_for_known_type(
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
    _merge_currency(row, plan, attributes)
    _merge_nominal(row, plan, attributes)
    jev_raw = dict(attributes.get("jev") or {})
    jev_raw.update(raw)
    attributes["jev"] = jev_raw
    row.attributes = attributes
    refresh_combination_status(row)


def _plan_for_effective_type(
    view: rules.DatasetView,
    tree: ConceptTree,
    plan: rules.CombinationPlan,
    row: MeasureCombination,
) -> rules.CombinationPlan:
    """Recompute a plan's derived fields for a row's stored (jev/manual) type.

    When the rules do not decide a type, the stored jev/manual type is still the
    EFFECTIVE one; currency/nominal/aggregation must follow it, not ``None``.
    """
    code_views = code_views_for(view, plan.codes) or []
    return rules.derived_combination(
        view,
        tree,
        plan.codes,
        plan.label,
        code_views,
        measure_type=row.measure_type,
        data_nature=row.data_nature,
        type_method=row.type_method,
        nature_method=row.nature_method,
        type_confidence=row.type_confidence,
        nature_confidence=row.nature_confidence,
        type_rule=plan.attributes.get("type_rule"),
        type_rule_tier=plan.attributes.get("type_rule_tier"),
        nature_rule=plan.attributes.get("nature_rule"),
    )


def upsert_combinations(
    session: Session,
    dataset: Dataset,
    plans: tuple[rules.CombinationPlan, ...],
    *,
    view: rules.DatasetView | None = None,
    tree: ConceptTree | None = None,
) -> dict[str, int]:
    """Apply combination plans field-level; delete stale unowned rows.

    Keys are canonical sorted JSON, matching the ``UNIQUE(dataset_id, codes)``
    constraint, so re-running is idempotent (no duplicate rows). A rule value
    replaces a field only when the field was empty or rule-owned, so Jev/manual
    answers survive a rules re-run. When a row's type came from Jev/manual and
    the rules do not decide it, the derived fields follow that effective type.
    """
    existing = {
        _combination_key(dict(row.codes or {})): row
        for row in session.scalars(
            sa.select(MeasureCombination).where(MeasureCombination.dataset_id == dataset.id)
        )
    }
    counts = {"inserted": 0, "updated": 0, "kept": 0, "deleted": 0}
    for plan in plans:
        key = _combination_key(plan.codes)
        row = existing.get(key)
        if (
            row is not None
            and view is not None
            and tree is not None
            and plan.measure_type is None
            and row.measure_type is not None
            and row.type_method in ("jev", "manual")
        ):
            plan = _plan_for_effective_type(view, tree, plan, row)
        if row is None:
            created = MeasureCombination(
                dataset_id=dataset.id,
                codes=plan.codes,
                label=plan.label,
                measure_type=plan.measure_type,
                data_nature=plan.data_nature,
                aggregation=plan.aggregation,
                source_aggregation=plan.source_aggregation,
                aggregation_conflict=plan.aggregation_conflict,
                para_birimi=plan.para_birimi,
                nominal_mi=plan.nominal_mi,
                mevsim_arindirilmis=plan.mevsim_arindirilmis,
                kumulatif=plan.kumulatif,
                type_method=plan.type_method,
                nature_method=plan.nature_method,
                type_confidence=plan.type_confidence,
                nature_confidence=plan.nature_confidence,
                status="pending",
                attributes=dict(plan.attributes),
            )
            refresh_combination_status(created)
            session.add(created)
            counts["inserted"] += 1
            continue
        if _row_locked(row):
            counts["kept"] += 1
            continue
        _merge_plan_into_row(row, plan)
        counts["updated"] += 1

    # A changed measure enumeration (e.g. Jev decided an undecided dimension)
    # leaves rows whose code key set no longer exists. Delete the unowned ones;
    # a manual/reviewed row is kept. An empty plan (enumeration guard fired) must
    # not wipe the dataset.
    if plans:
        valid = {_combination_key(plan.codes) for plan in plans}
        for key, row in existing.items():
            if key in valid:
                continue
            if _is_owned(row):
                counts["kept"] += 1
                continue
            session.delete(row)
            counts["deleted"] += 1
    session.flush()
    return counts


def recompute_dataset_flags(session: Session, dataset: Dataset) -> None:
    """Set the dataset array flags from its ACTUAL combinations (union).

    Reads the rows back from the database (the upsert flushed them) so Jev/manual
    values count, plus the SEASONAL_ADJUST dimension rule.
    """
    combinations = session.scalars(
        sa.select(MeasureCombination).where(MeasureCombination.dataset_id == dataset.id)
    ).all()
    currencies = sorted({row.para_birimi for row in combinations if row.para_birimi})
    nominal = sorted({row.nominal_mi for row in combinations if row.nominal_mi})
    seasonal: set[str] = {
        row.mevsim_arindirilmis for row in combinations if row.mevsim_arindirilmis
    }
    for dimension in dataset.dimensions:
        if "seasonal_adjust" in rules.fold(dimension.code):
            seasonal |= rules.seasonal_from_dimension(dimension)
    dataset.para_birimi = currencies
    dataset.nominal_mi = nominal
    dataset.mevsim_arindirilmis = sorted(seasonal)
    dataset.flags_checked_at = datetime.now(UTC)
    session.flush()


def apply_dataset_plan(
    session: Session,
    dataset: Dataset,
    plan: rules.DatasetPlan,
    *,
    period_candidate: str | None,
    view: rules.DatasetView | None = None,
    tree: ConceptTree | None = None,
) -> dict[str, int]:
    """Persist one dataset plan (description, dimensions, combinations, flags).

    ``donem_serisi``/``donem_serisi_grubu`` are owned by the Jev period pass and
    are never touched here (a rules re-run must not clear a confirmed series).
    """
    attributes = dict(dataset.attributes or {})
    if "source_description" not in attributes:
        attributes["source_description"] = plan.source_description
    elif plan.source_description is not None:
        attributes["source_description"] = plan.source_description
    if period_candidate is not None:
        attributes["period_series_candidate"] = period_candidate
    attributes.update(plan.attributes)
    # Stale pending markers must clear once the rules resolve them.
    for key in ("dims_pending", "measure_enrich_note"):
        if not plan.attributes.get(key):
            attributes.pop(key, None)
    dataset.attributes = attributes
    dataset.description = plan.description
    dataset.revizyon_tablosu = plan.revizyon_tablosu
    dataset.arsiv = plan.arsiv
    dataset.cok_konulu_derleme = plan.cok_konulu_derleme
    dataset.veri_yok = plan.veri_yok

    for dimension in dataset.dimensions:
        if dimension.code not in plan.measure_dims:
            continue
        if dimension.measure_source not in (None, "rule"):
            continue
        value = plan.measure_dims[dimension.code]
        dimension.is_measure = value
        dimension.measure_source = "rule" if value is not None else None

    counts = upsert_combinations(session, dataset, plan.combinations, view=view, tree=tree)
    recompute_dataset_flags(session, dataset)
    return counts


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


@dataclass
class Summary:
    """Collected counts for the ``rules``/``report`` output."""

    datasets: int = 0
    measure_types: Counter = field(default_factory=Counter)
    data_natures: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    combinations: int = 0
    deleted_combinations: int = 0
    pending_type: int = 0
    pending_nature: int = 0
    conflicts: int = 0
    pending_datasets: int = 0
    errors: list[str] = field(default_factory=list)

    def to_lines(self) -> list[str]:
        lines = [
            f"datasets: {self.datasets}",
            f"combinations: {self.combinations}",
            f"combinations deleted (stale): {self.deleted_combinations}",
            "measure_type: " + _counter_line(self.measure_types, include_none=True),
            "data_nature: " + _counter_line(self.data_natures, include_none=True),
            "flags: " + _counter_line(self.flags),
            f"pending measure_type: {self.pending_type}",
            f"pending data_nature: {self.pending_nature}",
            f"aggregation conflicts: {self.conflicts}",
            f"datasets without flags_checked_at: {self.pending_datasets}",
        ]
        if self.errors:
            lines.append(f"errors: {len(self.errors)}")
            lines.extend(f"  - {error}" for error in self.errors)
        return lines


def _counter_line(counter: Counter, *, include_none: bool = False) -> str:
    items = []
    for key, value in sorted(counter.items(), key=lambda item: str(item[0])):
        if key is None and not include_none:
            continue
        items.append(f"{key if key is not None else 'NULL'}={value}")
    return ", ".join(items) if items else "(none)"


def summarize_plans(views: list[rules.DatasetView], tree: ConceptTree) -> Summary:
    """Tally the rule results without touching the database."""
    summary = Summary(datasets=len(views))
    for view in views:
        plan = rules.compute_dataset_plan(view, tree)
        summary.flags["revizyon_tablosu"] += int(plan.revizyon_tablosu)
        summary.flags["arsiv"] += int(plan.arsiv)
        summary.flags["cok_konulu_derleme"] += int(plan.cok_konulu_derleme)
        summary.flags["veri_yok"] += int(plan.veri_yok)
        if plan.attributes.get("measure_enrich_note"):
            summary.flags["measure_enrich_skipped"] += 1
        if plan.attributes.get("dims_pending"):
            summary.flags["dims_pending"] += 1
        for combination in plan.combinations:
            summary.combinations += 1
            summary.measure_types[combination.measure_type] += 1
            summary.data_natures[combination.data_nature] += 1
            summary.pending_type += int(combination.measure_type is None)
            summary.pending_nature += int(combination.data_nature is None)
            summary.conflicts += int(combination.aggregation_conflict)
    return summary


def summarize_db(session: Session) -> Summary:
    """Read the same counts back from the database (``report``)."""
    summary = Summary()
    summary.datasets = session.scalar(sa.select(sa.func.count()).select_from(Dataset)) or 0
    summary.pending_datasets = (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(Dataset)
            .where(Dataset.flags_checked_at.is_(None))
        )
        or 0
    )
    for value, count in session.execute(
        sa.select(Dataset.revizyon_tablosu, sa.func.count()).group_by(Dataset.revizyon_tablosu)
    ):
        if value:
            summary.flags["revizyon_tablosu"] = count
    for value, count in session.execute(
        sa.select(Dataset.arsiv, sa.func.count()).group_by(Dataset.arsiv)
    ):
        if value:
            summary.flags["arsiv"] = count
    for value, count in session.execute(
        sa.select(Dataset.cok_konulu_derleme, sa.func.count()).group_by(Dataset.cok_konulu_derleme)
    ):
        if value:
            summary.flags["cok_konulu_derleme"] = count
    for value, count in session.execute(
        sa.select(Dataset.veri_yok, sa.func.count()).group_by(Dataset.veri_yok)
    ):
        if value:
            summary.flags["veri_yok"] = count
    summary.combinations = (
        session.scalar(sa.select(sa.func.count()).select_from(MeasureCombination)) or 0
    )
    for value, count in session.execute(
        sa.select(MeasureCombination.measure_type, sa.func.count()).group_by(
            MeasureCombination.measure_type
        )
    ):
        summary.measure_types[value] = count
    for value, count in session.execute(
        sa.select(MeasureCombination.data_nature, sa.func.count()).group_by(
            MeasureCombination.data_nature
        )
    ):
        summary.data_natures[value] = count
    summary.pending_type = (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(MeasureCombination)
            .where(MeasureCombination.measure_type.is_(None))
        )
        or 0
    )
    summary.pending_nature = (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(MeasureCombination)
            .where(MeasureCombination.data_nature.is_(None))
        )
        or 0
    )
    summary.conflicts = (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(MeasureCombination)
            .where(MeasureCombination.aggregation_conflict.is_(True))
        )
        or 0
    )
    return summary


def period_candidate_rows(session: Session) -> list[tuple[str, str]]:
    """Datasets carrying ``attributes['period_series_candidate']`` (code, group)."""
    rows = session.execute(
        sa.select(Dataset.external_code, Dataset.attributes["period_series_candidate"].astext)
        .where(Dataset.attributes["period_series_candidate"].astext.isnot(None))
        .order_by(Dataset.external_code)
    ).all()
    return [(code, group) for code, group in rows]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def _run_rules(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        summary, candidates = run_rules(
            session,
            dataset_code=args.dataset,
            institution=args.institution,
            dry_run=args.dry_run,
        )
        _print(summary, period_groups=candidates)
        return 1 if summary.errors else 0


def run_rules(
    session: Session,
    *,
    dataset_code: str | None = None,
    institution: str | None = None,
    dry_run: bool = False,
) -> tuple[Summary, dict[tuple[str, str], str]]:
    """Run the rule pass over the filtered datasets.

    Persists (unless ``dry_run``) committing per dataset, and returns the summary
    plus the period-series candidate map. A failing dataset is recorded in
    ``summary.errors`` and never aborts the run.
    """
    tree = load_tree()
    views = load_views(session, dataset_code=dataset_code, institution=institution)
    candidates = rules.period_series_candidates(load_all_dataset_names(session))
    if dry_run:
        return summarize_plans(views, tree), candidates

    by_code = {(view.institution_code, view.external_code): view for view in views}
    errors: list[str] = []
    deleted = 0
    processed = 0
    for (institution_code, external_code), view in by_code.items():
        try:
            dataset = session.scalar(
                sa.select(Dataset)
                .join(Institution, Dataset.institution_id == Institution.id)
                .where(
                    Institution.code == institution_code,
                    Dataset.external_code == external_code,
                )
                .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
            )
            if dataset is None:
                continue
            plan = rules.compute_dataset_plan(view, tree)
            counts = apply_dataset_plan(
                session,
                dataset,
                plan,
                period_candidate=candidates.get((institution_code, external_code)),
                view=view,
                tree=tree,
            )
            deleted += counts.get("deleted", 0)
            session.commit()
            processed += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            session.rollback()
            errors.append(f"{institution_code}/{external_code}: {exc}")
            logger.exception("enrich failed for %s/%s", institution_code, external_code)

    summary = summarize_plans(views, tree)
    summary.datasets = processed
    summary.deleted_combinations = deleted
    summary.errors = errors
    return summary, candidates


def _run_report(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        summary = summarize_db(session)
        groups = Counter(group for _, group in period_candidate_rows(session))
        _print(summary, period_groups=groups)
        for line in enrich_jev.report_lines(session):
            print(line)
    return 0


def _run_jev(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    kwargs: dict[str, Any] = {}
    if not args.dry_run:
        kwargs["jev_factory"] = enrich_jev.default_jev_factory
    run = enrich_jev.JevPass(
        session_factory=SessionLocal,
        workers=args.workers,
        limit=args.limit,
        institution=args.institution,
        dataset=args.dataset,
        dry_run=args.dry_run,
        **kwargs,
    )
    summary = run.run(args.step)
    for step, counts in summary.steps.items():
        print(
            f"step={step} processed={counts['processed']} accepted={counts['accepted']} "
            f"review={counts['review']} errors={counts['errors']}"
        )
    if summary.aborted:
        print("run aborted after too many consecutive failures")
        return 2
    print(f"errors: {summary.errors}")
    return 1 if summary.errors else 0


def _run_refresh_status(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        counts = enrich_jev.refresh_statuses(
            session,
            institution=args.institution,
            dataset=args.dataset,
            limit=args.limit,
        )
        session.commit()
    print(
        f"refreshed combinations={counts['combinations']} dims={counts['dims']} "
        f"periods={counts['periods']} nominal={counts['nominal']}"
    )
    return 0


def _run_review_list(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        rows = enrich_jev.review_rows(session, kind=args.kind, limit=args.limit)
    for row in rows:
        print("\t".join(row))
    return 0


def _run_set_combination(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        try:
            enrich_jev.set_combination(
                session,
                args.id,
                measure_type=args.type,
                data_nature=args.nature,
                currency=args.currency,
                nominal=args.nominal,
            )
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        session.commit()
    return 0


def _run_set_dimension(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        try:
            enrich_jev.set_dimension(session, args.id, measure=args.measure == "true")
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        session.commit()
    return 0


def _run_set_period(args: argparse.Namespace) -> int:
    from app.catalog import enrich_jev
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        count = enrich_jev.set_period(session, args.groupkey, accepted=bool(args.yes))
        session.commit()
    print(f"period group={args.groupkey} members={count}")
    return 0


def _print(summary: Summary, *, period_groups: dict[tuple[str, str], str] | Counter) -> None:
    for line in summary.to_lines():
        print(line)
    if isinstance(period_groups, Counter):
        counts = period_groups
    else:
        counts = Counter(period_groups.values())
    print(f"period-series candidate groups: {len(counts)}")
    for group, count in sorted(counts.items()):
        print(f"  {group}: {count}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.enrich",
        description="Deterministic catalog enrichment (Task 2.2a).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("rules", help="run and persist the deterministic rules")
    run.add_argument("--dataset", default=None, help="only this dataset external code")
    run.add_argument(
        "--institution",
        choices=("tuik", "tcmb", "hmb"),
        default=None,
        help="only datasets of this institution",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="compute and print the summary but write nothing",
    )

    subparsers.add_parser("report", help="print the same summary read from the database")

    jev = subparsers.add_parser("jev", help="run the Jev pass over the pending rows")
    jev.add_argument(
        "--step",
        choices=("dims", "types", "currency", "periods", "all"),
        default="all",
        help="which step to run (default: all)",
    )
    jev.add_argument("--institution", default=None, help="only datasets of this institution")
    jev.add_argument("--dataset", default=None, help="only this dataset external code")
    jev.add_argument("--limit", type=int, default=None, help="cap the number of items")
    jev.add_argument("--workers", type=int, default=4, help="worker pool size (default 4)")
    jev.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="print the states/questions without calling Jev or writing",
    )

    refresh = subparsers.add_parser(
        "refresh-status",
        help="recompute statuses from stored confidences with the current thresholds",
    )
    refresh.add_argument("--institution", default=None)
    refresh.add_argument("--dataset", default=None)
    refresh.add_argument("--limit", type=int, default=None)

    review = subparsers.add_parser("review-list", help="print review items as TSV")
    review.add_argument(
        "--kind",
        choices=("dims", "types", "currency", "nominal", "periods"),
        default="types",
    )
    review.add_argument("--limit", type=int, default=None)

    set_combination = subparsers.add_parser("set-combination", help="manual combination override")
    set_combination.add_argument("id", type=int)
    set_combination.add_argument("--type", default=None)
    set_combination.add_argument("--nature", default=None)
    set_combination.add_argument("--currency", default=None)
    set_combination.add_argument("--nominal", choices=("reel", "nominal"), default=None)

    set_dimension = subparsers.add_parser("set-dimension", help="manual dimension override")
    set_dimension.add_argument("id", type=int)
    set_dimension.add_argument("--measure", choices=("true", "false"), required=True)

    set_period = subparsers.add_parser("set-period", help="manual period-series decision")
    set_period.add_argument("groupkey")
    group = set_period.add_mutually_exclusive_group(required=True)
    group.add_argument("--yes", action="store_true")
    group.add_argument("--no", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    if args.command == "rules":
        return _run_rules(args)
    if args.command == "report":
        return _run_report(args)
    if args.command == "jev":
        return _run_jev(args)
    if args.command == "refresh-status":
        return _run_refresh_status(args)
    if args.command == "review-list":
        return _run_review_list(args)
    if args.command == "set-combination":
        return _run_set_combination(args)
    if args.command == "set-dimension":
        return _run_set_dimension(args)
    if args.command == "set-period":
        return _run_set_period(args)
    raise SystemExit(2)


if __name__ == "__main__":
    sys.exit(main())
