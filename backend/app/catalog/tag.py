"""Dataset topic tagging with Jev (Task 2.3): pure decision logic + persistence.

The concept tree (Task 2.1) defines 24 branches and 109 leaves. Every dataset is
scored in two Jev stages: first the 24 branches in one ``noul`` call, then only
the leaves of the branches that passed. The pure :func:`decide_tags` turns the
scores into ``dataset_tags`` rows and review reasons; it never calls Jev, so the
stored scores can be re-decided with other thresholds by ``refresh``.

Thresholds (one source of truth here, overridable from the CLI for calibration):

- ``BRANCH_PASS = 0.40``: a branch with a yes-probability at or above this passes
  and its leaves are asked. No count limit.
- ``LEAF_ACCEPT = 0.60``: a leaf at or above this is a candidate tag. Up to three
  become ``accepted``; extra candidates are stored ``rejected`` with their rank.
- ``cok_konulu_derleme`` datasets (Turcat, TCMB PKA) get only branch tags, using
  the same 0.60 accept threshold.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from sqlalchemy.orm import Session

from app.catalog.enrich_jev import category_text
from app.catalog.tree import ConceptTree, load_tree
from app.data.models import Dataset, DatasetTag, Institution

logger = logging.getLogger("app.catalog.tag")

#: A branch at or above this yes-probability passes and its leaves are asked.
BRANCH_PASS = 0.40
#: A leaf at or above this yes-probability is an accepted-tag candidate.
LEAF_ACCEPT = 0.60
#: At most this many leaf tags per dataset (>3 candidates are cut to ``rejected``).
MAX_LEAF_TAGS = 3
#: How many below-threshold leaves/branches a zero-candidate dataset stores for review.
REVIEW_CANDIDATE_LIMIT = 3
#: ``cok_konulu_derleme`` datasets accept a branch at the same threshold.
COK_KONULU_ACCEPT = LEAF_ACCEPT
#: Never send more than this many questions in one Jev call.
MAX_QUESTIONS_PER_CALL = 40

PROMPT_DIR = Path(__file__).parent / "prompts"
HINTS_PATH = Path(__file__).parent / "category_hints.yaml"

_ACCEPTED = "accepted"
_REVIEW = "review"
_REJECTED = "rejected"


# --------------------------------------------------------------------------- #
# Pure decision logic
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TagDecision:
    """One ``dataset_tags`` row the decision produced (source is always Jev)."""

    tag_id: str
    level: str
    confidence: float
    status: str
    note: str | None = None


@dataclass(frozen=True)
class Decision:
    """The full outcome for one dataset: tag rows plus review reasons."""

    rows: tuple[TagDecision, ...] = ()
    reasons: tuple[str, ...] = ()
    skipped: bool = False


def _sorted_leaves(leaf_scores: Mapping[str, float], tree: ConceptTree) -> list[str]:
    return sorted(
        leaf_scores,
        key=lambda leaf: (
            -float(leaf_scores[leaf]),
            tree.leaf_position.get(leaf, sys.maxsize),
        ),
    )


def _sorted_branches(branch_scores: Mapping[str, float], tree: ConceptTree) -> list[str]:
    return sorted(
        branch_scores,
        key=lambda branch: (
            -float(branch_scores[branch]),
            tree.branch_position.get(branch, sys.maxsize),
        ),
    )


def _decide_normal(
    branch_scores: Mapping[str, float],
    leaf_scores: Mapping[str, float],
    branch_pass: float,
    leaf_accept: float,
    tree: ConceptTree,
) -> Decision:
    ordered = _sorted_leaves(leaf_scores, tree)
    candidates = [leaf for leaf in ordered if float(leaf_scores[leaf]) >= leaf_accept]

    if candidates:
        rows = [
            TagDecision(leaf, "leaf", float(leaf_scores[leaf]), _ACCEPTED)
            for leaf in candidates[:MAX_LEAF_TAGS]
        ]
        for rank, leaf in enumerate(candidates[MAX_LEAF_TAGS:], start=MAX_LEAF_TAGS + 1):
            rows.append(
                TagDecision(
                    leaf,
                    "leaf",
                    float(leaf_scores[leaf]),
                    _REJECTED,
                    f"kesildi: {rank}. sıra",
                )
            )
        return Decision(rows=tuple(rows))

    # Zero candidates: every dataset still ends up with accepted or review rows.
    rows: list[TagDecision] = []
    reasons = ["yaprak yok"]
    best_leaf = max((float(score) for score in leaf_scores.values()), default=None)
    above = [leaf for leaf in ordered if float(leaf_scores[leaf]) >= branch_pass][
        :REVIEW_CANDIDATE_LIMIT
    ]
    if above:
        for leaf in above:
            rows.append(
                TagDecision(leaf, "leaf", float(leaf_scores[leaf]), _REVIEW, "yaprak yok")
            )
    elif leaf_scores:
        best = ordered[0]
        rows.append(TagDecision(best, "leaf", float(leaf_scores[best]), _REVIEW, "yaprak yok"))

    if best_leaf is not None and best_leaf < branch_pass:
        reasons.append("bosluk")
        rows = [
            replace(row, note=f"boşluk: en yüksek yaprak {row.confidence:.2f}")
            if row.level == "leaf"
            else row
            for row in rows
        ]

    high_branches = [
        branch
        for branch in _sorted_branches(branch_scores, tree)
        if float(branch_scores[branch]) >= leaf_accept
    ]
    for branch in high_branches:
        rows.append(
            TagDecision(
                branch,
                "branch",
                float(branch_scores[branch]),
                _REVIEW,
                "dal yüksek, yaprak yok",
            )
        )
    if high_branches:
        reasons.append("dal_yaprak_yok")

    if not rows and branch_scores:
        # No leaves were even asked (no branch passed): fall back to the best branch.
        best_branch = _sorted_branches(branch_scores, tree)[0]
        score = float(branch_scores[best_branch])
        if score < branch_pass:
            reasons.append("bosluk")
            rows.append(
                TagDecision(
                    best_branch,
                    "branch",
                    score,
                    _REVIEW,
                    f"boşluk: en yüksek dal {score:.2f}",
                )
            )
        else:
            rows.append(TagDecision(best_branch, "branch", score, _REVIEW, "yaprak yok"))

    return Decision(rows=tuple(rows), reasons=tuple(reasons))


def _decide_branch_only(
    branch_scores: Mapping[str, float],
    branch_pass: float,
    leaf_accept: float,
    tree: ConceptTree,
) -> Decision:
    ordered = _sorted_branches(branch_scores, tree)
    accepted = [
        branch for branch in ordered if float(branch_scores[branch]) >= leaf_accept
    ]
    if accepted:
        rows = tuple(
            TagDecision(branch, "branch", float(branch_scores[branch]), _ACCEPTED)
            for branch in accepted
        )
        return Decision(rows=rows)
    if not ordered:
        return Decision(reasons=("yaprak yok",))
    best = ordered[0]
    score = float(branch_scores[best])
    if score < branch_pass:
        return Decision(
            rows=(
                TagDecision(
                    best,
                    "branch",
                    score,
                    _REVIEW,
                    f"boşluk: en yüksek dal {score:.2f}",
                ),
            ),
            reasons=("yaprak yok", "bosluk"),
        )
    return Decision(
        rows=(TagDecision(best, "branch", score, _REVIEW, "yaprak yok"),),
        reasons=("yaprak yok",),
    )


def _apply_hint(
    decision: Decision,
    hint_branches: Collection[str] | None,
    tree: ConceptTree,
) -> Decision:
    if decision.skipped or not decision.rows or hint_branches is None:
        return decision
    accepted_branches = {
        branch
        for branch in (
            tree.branch_of(row.tag_id)
            for row in decision.rows
            if row.status == _ACCEPTED
        )
        if branch is not None
    }
    if not accepted_branches:
        return decision
    hint = set(hint_branches)
    if accepted_branches & hint:
        return decision
    note = "kategori ipucu uyuşmuyor: ipucu=" + ",".join(sorted(hint))
    rows = tuple(
        replace(row, status=_REVIEW, note=note) if row.status != _REJECTED else row
        for row in decision.rows
    )
    reasons = tuple(dict.fromkeys((*decision.reasons, "kategori_uyusmazligi")))
    return Decision(rows=rows, reasons=reasons)


def decide_tags(
    *,
    branch_scores: Mapping[str, float],
    leaf_scores: Mapping[str, float],
    cok_konulu: bool,
    hint_branches: Collection[str] | None = None,
    branch_pass: float = BRANCH_PASS,
    leaf_accept: float = LEAF_ACCEPT,
    has_manual: bool = False,
    tree: ConceptTree | None = None,
) -> Decision:
    """Turn Jev scores into tag rows + review reasons (no I/O, no Jev).

    ``hint_branches`` is the branch set of the dataset's category hint, or
    ``None`` when the source category has no hint entry (then no check runs).
    """
    if has_manual:
        return Decision(skipped=True)
    concept_tree = tree or load_tree()
    if cok_konulu:
        decision = _decide_branch_only(branch_scores, branch_pass, leaf_accept, concept_tree)
    else:
        decision = _decide_normal(
            branch_scores, leaf_scores, branch_pass, leaf_accept, concept_tree
        )
    return _apply_hint(decision, hint_branches, concept_tree)


# --------------------------------------------------------------------------- #
# Category hints (generated by Jev, hand-editable)
# --------------------------------------------------------------------------- #


def hint_key(institution_code: str, category: str) -> str:
    """The stable key of one ``(institution, category_text)`` hint entry."""
    return f"{institution_code}|{category}"


def _read_hints(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"category hints file {path} is not a mapping")
    hints: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        branches = entry.get("branches") or {}
        hints[str(key)] = {
            "branches": {str(branch): float(score) for branch, score in branches.items()},
            "manual": bool(entry.get("manual", False)),
        }
    return hints


@lru_cache(maxsize=1)
def load_category_hints() -> dict[str, dict[str, Any]]:
    """Load and cache the packaged category hints (empty when the file is empty)."""
    return _read_hints(HINTS_PATH)


def hint_branches_for(
    hints: Mapping[str, dict[str, Any]], institution_code: str, category: str
) -> frozenset[str] | None:
    """The hint branch set for a category, or ``None`` when there is no entry."""
    entry = hints.get(hint_key(institution_code, category))
    if entry is None:
        return None
    return frozenset(entry["branches"])


def merge_hints(
    existing: Mapping[str, Mapping[str, Any]],
    generated: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Regenerate hints but keep every ``manual: true`` entry untouched."""
    merged: dict[str, dict[str, Any]] = {}
    for key in sorted(set(existing) | set(generated)):
        if existing.get(key, {}).get("manual"):
            merged[key] = dict(existing[key])
        elif key in generated:
            merged[key] = {
                "branches": dict(generated[key]["branches"]),
                "manual": False,
            }
    return merged


def write_hints(path: Path, merged: Mapping[str, Mapping[str, Any]]) -> None:
    """Write hints as UTF-8 YAML with sorted top-level keys for readable diffs."""
    ordered = {key: merged[key] for key in sorted(merged)}
    text = yaml.safe_dump(
        ordered, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _validate_tag(tag_id: str, level: str, tree: ConceptTree) -> None:
    if level == "leaf":
        if tag_id not in tree.leaf_ids:
            raise ValueError(f"unknown leaf tag id {tag_id!r}")
    elif level == "branch":
        if tag_id not in tree.branch_ids:
            raise ValueError(f"unknown branch tag id {tag_id!r}")
    else:
        raise ValueError(f"unknown tag level {level!r}")


def has_manual_tags(session: Session, dataset_id: int) -> bool:
    """True when a human owns at least one tag of this dataset."""
    count = session.scalar(
        sa.select(sa.func.count())
        .select_from(DatasetTag)
        .where(DatasetTag.dataset_id == dataset_id, DatasetTag.source == "manual")
    )
    return bool(count)


def replace_jev_tags(
    session: Session,
    dataset_id: int,
    rows: Sequence[TagDecision],
    *,
    tree: ConceptTree,
) -> None:
    """Delete this dataset's Jev rows and write the new ones (one dataset's write)."""
    session.execute(
        sa.delete(DatasetTag).where(
            DatasetTag.dataset_id == dataset_id, DatasetTag.source == "jev"
        )
    )
    for row in rows:
        _validate_tag(row.tag_id, row.level, tree)
        if row.status not in {_ACCEPTED, _REVIEW, _REJECTED}:
            raise ValueError(f"unknown tag status {row.status!r}")
        session.add(
            DatasetTag(
                dataset_id=dataset_id,
                tag_id=row.tag_id,
                level=row.level,
                confidence=float(row.confidence),
                source="jev",
                status=row.status,
                note=row.note,
            )
        )
    session.flush()


def persist_decision(
    session: Session,
    dataset: Dataset,
    decision: Decision,
    *,
    branch_scores: Mapping[str, float],
    leaf_scores: Mapping[str, float],
    prompt_versions: Mapping[str, Any],
    thresholds: Mapping[str, float],
    tagged_at: str,
    tree: ConceptTree,
) -> None:
    """Write the tag rows and store the scores so ``refresh`` needs no Jev call."""
    replace_jev_tags(session, dataset.id, decision.rows, tree=tree)
    attributes = dict(dataset.attributes or {})
    attributes["tagging"] = {
        "branch_scores": {key: float(value) for key, value in branch_scores.items()},
        "leaf_scores": {key: float(value) for key, value in leaf_scores.items()},
        "prompt_versions": dict(prompt_versions),
        "thresholds": {
            "branch_pass": float(thresholds["branch_pass"]),
            "leaf_accept": float(thresholds["leaf_accept"]),
        },
        "reasons": list(decision.reasons),
        "tagged_at": tagged_at,
    }
    dataset.attributes = attributes
    session.flush()


def set_manual_tags(
    session: Session,
    dataset: Dataset,
    *,
    leaves: Sequence[str] | None = None,
    branches: Sequence[str] | None = None,
    tree: ConceptTree | None = None,
) -> list[TagDecision]:
    """Replace a dataset's tags with human tags (``source='manual'``)."""
    concept_tree = tree or load_tree()
    leaves = list(leaves or ())
    branches = list(branches or ())
    if leaves and branches:
        raise ValueError("give --leaf or --branch, not both")
    if not leaves and not branches:
        raise ValueError("give --leaf and/or --branch")
    if leaves and not dataset.cok_konulu_derleme and len(leaves) > MAX_LEAF_TAGS:
        raise ValueError(
            f"at most {MAX_LEAF_TAGS} leaves unless the dataset is a cok_konulu_derleme"
        )
    rows = [TagDecision(leaf, "leaf", 1.0, _ACCEPTED) for leaf in leaves]
    rows += [TagDecision(branch, "branch", 1.0, _ACCEPTED) for branch in branches]
    for row in rows:
        _validate_tag(row.tag_id, row.level, concept_tree)
    session.execute(sa.delete(DatasetTag).where(DatasetTag.dataset_id == dataset.id))
    for row in rows:
        session.add(
            DatasetTag(
                dataset_id=dataset.id,
                tag_id=row.tag_id,
                level=row.level,
                confidence=1.0,
                source="manual",
                status=_ACCEPTED,
                note=None,
            )
        )
    session.flush()
    return rows


def tagged_at(now: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp for the ``tagging`` attribute."""
    return (now or datetime.now(UTC)).isoformat()


# --------------------------------------------------------------------------- #
# Refresh (recompute from stored scores, no Jev)
# --------------------------------------------------------------------------- #


def refresh(
    session: Session,
    *,
    branch_pass: float = BRANCH_PASS,
    leaf_accept: float = LEAF_ACCEPT,
    institution: str | None = None,
    dataset: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Re-decide every stored dataset's rows with the given thresholds."""
    tree = load_tree()
    hints = load_category_hints()
    statement = (
        sa.select(Dataset, Institution.code)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(Dataset.attributes["tagging"].astext.isnot(None))
        .order_by(Dataset.id)
    )
    if institution:
        statement = statement.where(Institution.code == institution)
    if dataset:
        statement = statement.where(Dataset.external_code == dataset)
    if limit:
        statement = statement.limit(limit)

    counts = {"datasets": 0, "accepted": 0, "review": 0, "skipped": 0}
    for row in session.execute(statement).all():
        db_dataset, institution_code = row
        if has_manual_tags(session, db_dataset.id):
            counts["skipped"] += 1
            continue
        tagging = dict((db_dataset.attributes or {}).get("tagging") or {})
        branch_scores = {
            key: float(value)
            for key, value in (tagging.get("branch_scores") or {}).items()
        }
        leaf_scores = {
            key: float(value)
            for key, value in (tagging.get("leaf_scores") or {}).items()
        }
        category = category_text(db_dataset, institution_code)
        decision = decide_tags(
            branch_scores=branch_scores,
            leaf_scores=leaf_scores,
            cok_konulu=bool(db_dataset.cok_konulu_derleme),
            hint_branches=hint_branches_for(hints, institution_code, category),
            branch_pass=branch_pass,
            leaf_accept=leaf_accept,
            tree=tree,
        )
        replace_jev_tags(session, db_dataset.id, decision.rows, tree=tree)
        tagging["thresholds"] = {
            "branch_pass": float(branch_pass),
            "leaf_accept": float(leaf_accept),
        }
        tagging["reasons"] = list(decision.reasons)
        attributes = dict(db_dataset.attributes or {})
        attributes["tagging"] = tagging
        db_dataset.attributes = attributes
        counts["datasets"] += 1
        if any(item.status == _REVIEW for item in decision.rows):
            counts["review"] += 1
        else:
            counts["accepted"] += 1
    session.flush()
    return counts


# --------------------------------------------------------------------------- #
# Calibration sample
# --------------------------------------------------------------------------- #


def _resolve_include(
    identities: Mapping[str, tuple[str, bool]], tokens: Sequence[str]
) -> list[str]:
    """Resolve ``--include`` tokens (external code or full identity) to identities."""
    by_external: dict[str, list[str]] = {}
    for identity in identities:
        by_external.setdefault(identity.split(":", 1)[1], []).append(identity)
    resolved: list[str] = []
    missing: list[str] = []
    for token in tokens:
        if token in identities:
            resolved.append(token)
            continue
        matches = by_external.get(token, [])
        if len(matches) == 1:
            resolved.append(matches[0])
        elif not matches:
            missing.append(token)
        else:
            raise ValueError(f"ambiguous include {token!r}: {sorted(matches)}")
    if missing:
        raise ValueError(f"include codes not found: {', '.join(missing)}")
    return resolved


def build_sample(
    session: Session,
    *,
    size: int,
    seed: int,
    include: Sequence[str] | None = None,
) -> list[str]:
    """A deterministic stratified sample of ``institution:external_code`` lines."""
    rows = session.execute(
        sa.select(
            Institution.code,
            Dataset.external_code,
            Dataset.cok_konulu_derleme,
        ).join(Dataset, Dataset.institution_id == Institution.id)
    ).all()
    identities: dict[str, tuple[str, bool]] = {
        f"{institution_code}:{external_code}": (institution_code, bool(cok))
        for institution_code, external_code, cok in rows
    }
    include = _resolve_include(identities, include or ())
    fixed: list[str] = []
    seen: set[str] = set()

    def add(identity: str) -> None:
        if identity not in seen:
            seen.add(identity)
            fixed.append(identity)

    for identity in sorted(k for k, (code, _cok) in identities.items() if code == "hmb"):
        add(identity)
    for identity in sorted(k for k, (_code, cok) in identities.items() if cok):
        add(identity)
    for identity in include:
        add(identity)

    remaining = max(0, size - len(fixed))
    pool_tuik = sorted(
        identity
        for identity, (code, _cok) in identities.items()
        if code == "tuik" and identity not in seen
    )
    pool_tcmb = sorted(
        identity
        for identity, (code, _cok) in identities.items()
        if code == "tcmb" and identity not in seen
    )
    denominator = len(pool_tuik) + len(pool_tcmb)
    if remaining and denominator:
        n_tuik = min(round(remaining * len(pool_tuik) / denominator), len(pool_tuik))
        n_tcmb = min(remaining - n_tuik, len(pool_tcmb))
        n_tuik = min(remaining - n_tcmb, len(pool_tuik))
        rng = random.Random(seed)
        for identity in rng.sample(pool_tuik, n_tuik):
            add(identity)
        for identity in rng.sample(pool_tcmb, n_tcmb):
            add(identity)
    return sorted(fixed)


def read_sample_file(path: str | Path) -> list[str]:
    """Read ``institution:external_code`` lines, ignoring blanks and comments."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


# --------------------------------------------------------------------------- #
# Reporting (database only)
# --------------------------------------------------------------------------- #

_CONFIDENCE_BUCKETS = (
    (0.0, 0.4),
    (0.4, 0.5),
    (0.5, 0.6),
    (0.6, 0.7),
    (0.7, 0.8),
    (0.8, 0.9),
    (0.9, 1.0),
)


def _bucket(confidence: float) -> str:
    for low, high in _CONFIDENCE_BUCKETS:
        if low <= confidence < high + 1e-9:
            return f"{low:.1f}-{high:.1f}"
    return "0.9-1.0"


def report_lines(session: Session) -> list[str]:
    """Counts by status/institution plus confidence and coverage histograms."""
    lines: list[str] = []
    total_datasets = session.scalar(sa.select(sa.func.count()).select_from(Dataset)) or 0

    status_counts: Counter = Counter()
    for status, count in session.execute(
        sa.select(DatasetTag.status, sa.func.count()).group_by(DatasetTag.status)
    ):
        status_counts[status] += count

    by_institution: dict[str, Counter] = {}
    for institution_code, status, count in session.execute(
        sa.select(Institution.code, DatasetTag.status, sa.func.count())
        .join(Dataset, DatasetTag.dataset_id == Dataset.id)
        .join(Institution, Dataset.institution_id == Institution.id)
        .group_by(Institution.code, DatasetTag.status)
    ):
        by_institution.setdefault(institution_code, Counter())[status] += count

    tagged_datasets = {
        status: session.scalar(
            sa.select(sa.func.count(sa.distinct(DatasetTag.dataset_id))).where(
                DatasetTag.status == status
            )
        )
        or 0
        for status in (_ACCEPTED, _REVIEW, _REJECTED)
    }
    any_tagged = (
        session.scalar(sa.select(sa.func.count(sa.distinct(DatasetTag.dataset_id)))) or 0
    )
    untagged = max(0, total_datasets - any_tagged)

    reason_counts: Counter = Counter()
    bosluk_datasets = 0
    for attributes in session.scalars(sa.select(Dataset.attributes)).all():
        reasons = list(((attributes or {}).get("tagging") or {}).get("reasons") or [])
        for reason in reasons:
            reason_counts[reason] += 1
        if "bosluk" in reasons:
            bosluk_datasets += 1

    histogram: Counter = Counter()
    for confidence in session.scalars(
        sa.select(DatasetTag.confidence).where(
            DatasetTag.source == "jev",
            DatasetTag.level == "leaf",
            DatasetTag.status == _ACCEPTED,
            DatasetTag.confidence.isnot(None),
        )
    ).all():
        histogram[_bucket(float(confidence))] += 1

    cut_leaves: Counter = Counter()
    for tag_id, count in session.execute(
        sa.select(DatasetTag.tag_id, sa.func.count())
        .where(DatasetTag.status == _REJECTED)
        .group_by(DatasetTag.tag_id)
        .order_by(sa.func.count().desc())
    ):
        cut_leaves[tag_id] += count

    used_leaves = set(
        session.scalars(
            sa.select(DatasetTag.tag_id)
            .where(
                DatasetTag.source == "jev",
                DatasetTag.level == "leaf",
                DatasetTag.status == _ACCEPTED,
            )
            .distinct()
        ).all()
    )
    tree = load_tree()
    unused = sorted(tree.leaf_ids - used_leaves)

    lines.append(
        f"datasets: total={total_datasets} with_tags={any_tagged} untagged={untagged} "
        f"accepted={tagged_datasets[_ACCEPTED]} review={tagged_datasets[_REVIEW]} "
        f"rejected={tagged_datasets[_REJECTED]}"
    )
    lines.append(
        f"tags: accepted={status_counts.get(_ACCEPTED, 0)} "
        f"review={status_counts.get(_REVIEW, 0)} "
        f"rejected={status_counts.get(_REJECTED, 0)}"
    )
    for institution_code in sorted(by_institution):
        counts = by_institution[institution_code]
        lines.append(
            f"institution {institution_code}: accepted={counts.get(_ACCEPTED, 0)} "
            f"review={counts.get(_REVIEW, 0)} rejected={counts.get(_REJECTED, 0)}"
        )
    if reason_counts:
        for reason in sorted(reason_counts):
            lines.append(f"reason {reason}={reason_counts[reason]}")
    else:
        lines.append("reason none")
    lines.append(f"bosluk datasets: {bosluk_datasets}")
    bucket_text = ", ".join(
        f"{low:.1f}-{high:.1f}={histogram.get(f'{low:.1f}-{high:.1f}', 0)}"
        for low, high in _CONFIDENCE_BUCKETS
    )
    lines.append(f"accepted leaf confidence histogram: {bucket_text}")
    if cut_leaves:
        top = ", ".join(f"{leaf}={count}" for leaf, count in cut_leaves.most_common(10))
        lines.append(f"most cut leaves: {top}")
    else:
        lines.append("most cut leaves: none")
    lines.append(f"leaves never accepted ({len(unused)}): {', '.join(unused)}")
    return lines


def review_rows(session: Session, *, limit: int | None = None) -> list[list[str]]:
    """TSV rows for datasets with at least one review tag."""
    statement = (
        sa.select(Dataset, Institution.code)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(
            sa.exists().where(
                DatasetTag.dataset_id == Dataset.id, DatasetTag.status == _REVIEW
            )
        )
        .order_by(Dataset.id)
    )
    if limit:
        statement = statement.limit(limit)
    rows: list[list[str]] = []
    for dataset, institution_code in session.execute(statement).all():
        tagging = (dataset.attributes or {}).get("tagging") or {}
        leaf_scores = tagging.get("leaf_scores") or {}
        best = sorted(leaf_scores.items(), key=lambda item: -float(item[1]))[:3]
        best_text = "; ".join(f"{leaf}={float(score):.2f}" for leaf, score in best)
        rows.append(
            [
                dataset.external_code,
                institution_code,
                dataset.name,
                category_text(dataset, institution_code),
                ",".join(tagging.get("reasons") or []),
                best_text,
            ]
        )
    return rows


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_identity(identity: str) -> tuple[str, str]:
    if ":" not in identity:
        raise ValueError(f"dataset must be 'institution:external_code', got {identity!r}")
    institution_code, external_code = identity.split(":", 1)
    if not institution_code or not external_code:
        raise ValueError(f"dataset must be 'institution:external_code', got {identity!r}")
    return institution_code, external_code


def _load_dataset(session: Session, institution_code: str, external_code: str) -> Dataset:
    dataset = session.scalar(
        sa.select(Dataset)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(
            Institution.code == institution_code,
            Dataset.external_code == external_code,
        )
    )
    if dataset is None:
        raise ValueError(f"dataset {institution_code}:{external_code} not found")
    return dataset


def _run_run(args: argparse.Namespace) -> int:
    from app.catalog import tag_jev
    from app.db.session import SessionLocal

    identities = read_sample_file(args.sample_file) if args.sample_file else None
    kwargs: dict[str, Any] = {}
    if not args.dry_run:
        kwargs["jev_factory"] = tag_jev.default_jev_factory
    runner = tag_jev.TagPass(
        session_factory=SessionLocal,
        workers=args.workers,
        limit=args.limit,
        institution=args.institution,
        dataset=args.dataset,
        dry_run=args.dry_run,
        force=args.force,
        branch_pass=args.branch_pass,
        leaf_accept=args.leaf_accept,
        sample_identities=identities,
        **kwargs,
    )
    summary = runner.run()
    print(
        f"tagged={summary.tagged} accepted={summary.accepted} review={summary.review} "
        f"rejected={summary.rejected} errors={summary.errors} skipped={summary.skipped}"
    )
    if summary.aborted:
        print("run aborted after too many consecutive failures")
        return 2
    if summary.errors:
        return 1
    return 0


def _run_hints(args: argparse.Namespace) -> int:
    from app.catalog import tag_jev
    from app.db.session import SessionLocal

    generated = tag_jev.generate_hints(
        SessionLocal,
        jev_factory=tag_jev.default_jev_factory,
        workers=args.workers,
        branch_pass=args.branch_pass,
        out=print,
    )
    print(f"hints: categories={len(generated)}")
    return 0


def _run_sample(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        sample = build_sample(
            session, size=args.size, seed=args.seed, include=args.include
        )
    if args.out:
        Path(args.out).write_text("\n".join(sample) + "\n", encoding="utf-8")
        print(f"sample size={len(sample)} out={args.out}")
    else:
        for identity in sample:
            print(identity)
        print(f"sample size={len(sample)}")
    return 0


def _run_report(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        for line in report_lines(session):
            print(line)
    return 0


def _run_review_list(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        rows = review_rows(session, limit=args.limit)
    for row in rows:
        print("\t".join(row))
    return 0


def _run_refresh(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        counts = refresh(
            session,
            branch_pass=args.branch_pass,
            leaf_accept=args.leaf_accept,
            institution=args.institution,
            dataset=args.dataset,
            limit=args.limit,
        )
        session.commit()
    print(
        f"refreshed datasets={counts['datasets']} accepted={counts['accepted']} "
        f"review={counts['review']} skipped={counts['skipped']}"
    )
    return 0


def _run_set(args: argparse.Namespace) -> int:
    from app.db.session import SessionLocal

    try:
        institution_code, external_code = _parse_identity(args.dataset)
        leaves = [item for item in (args.leaf or "").split(",") if item] or None
        branches = [item for item in (args.branch or "").split(",") if item] or None
        with SessionLocal() as session:
            dataset = _load_dataset(session, institution_code, external_code)
            set_manual_tags(session, dataset, leaves=leaves, branches=branches)
            session.commit()
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(f"manual tags written for {args.dataset}")
    return 0


def _add_thresholds(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--branch-pass",
        type=float,
        default=BRANCH_PASS,
        help=f"branch pass threshold (default {BRANCH_PASS})",
    )
    parser.add_argument(
        "--leaf-accept",
        type=float,
        default=LEAF_ACCEPT,
        help=f"leaf accept threshold (default {LEAF_ACCEPT})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.tag",
        description="Topic tagging of datasets via Jev (Task 2.3).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="score and tag datasets via Jev")
    run.add_argument("--institution", default=None, help="only datasets of this institution")
    run.add_argument("--dataset", default=None, help="only this dataset external code")
    run.add_argument("--limit", type=int, default=None, help="cap the number of datasets")
    run.add_argument("--workers", type=int, default=4, help="worker pool size (default 4)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="print the planned calls without calling Jev or writing",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="re-tag datasets that already have a tagging attribute",
    )
    run.add_argument(
        "--sample-file",
        default=None,
        dest="sample_file",
        help="only the institution:external_code lines in this file",
    )
    _add_thresholds(run)

    hints = subparsers.add_parser("hints", help="regenerate category hints via Jev")
    hints.add_argument("--workers", type=int, default=4, help="worker pool size (default 4)")
    hints.add_argument(
        "--branch-pass",
        type=float,
        default=BRANCH_PASS,
        help=f"only branches at/above this score are stored (default {BRANCH_PASS})",
    )

    sample = subparsers.add_parser("sample", help="write a deterministic calibration sample")
    sample.add_argument("--size", type=int, default=200)
    sample.add_argument("--seed", type=int, default=42)
    sample.add_argument("--include", action="append", default=[], help="extra identity")
    sample.add_argument("--out", default=None, help="write the sample to this file")

    subparsers.add_parser("report", help="print tagging counts from the database")

    review = subparsers.add_parser("review-list", help="print review datasets as TSV")
    review.add_argument("--limit", type=int, default=None)

    refresh_parser = subparsers.add_parser(
        "refresh", help="recompute rows from stored scores without Jev"
    )
    refresh_parser.add_argument("--institution", default=None)
    refresh_parser.add_argument("--dataset", default=None)
    refresh_parser.add_argument("--limit", type=int, default=None)
    _add_thresholds(refresh_parser)

    set_parser = subparsers.add_parser("set", help="write manual tags for one dataset")
    set_parser.add_argument("--dataset", required=True, help="institution:external_code")
    group = set_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--leaf", default=None, help="comma-separated leaf ids")
    group.add_argument("--branch", default=None, help="comma-separated branch ids")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    handlers = {
        "run": _run_run,
        "hints": _run_hints,
        "sample": _run_sample,
        "report": _run_report,
        "review-list": _run_review_list,
        "refresh": _run_refresh,
        "set": _run_set,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
