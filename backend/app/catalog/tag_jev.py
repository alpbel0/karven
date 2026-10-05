"""The Jev tagging passes (Task 2.3): branch + leaf scoring and category hints.

Everything here talks to Jev (through the :class:`JevDecider` protocol) or builds
the request; the decision and persistence live in :mod:`app.catalog.tag`. The
verified-live question shape is a ``noul`` question with the branch/leaf name in
``instructions`` and ``criteria={"evet": ..., "hayır": ...}``; the answer's
``noul`` value is the yes-probability used directly as the score.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.catalog import enrich, linking
from app.catalog import enrich_rules as rules
from app.catalog.enrich_jev import (
    JevDecider,
    _answer_for,
    _clean_state,
    category_text,
    default_jev_factory,
    run_pool,
)
from app.catalog.tag import (
    BRANCH_PASS,
    HINTS_PATH,
    LEAF_ACCEPT,
    MAX_QUESTIONS_PER_CALL,
    Decision,
    decide_tags,
    has_manual_tags,
    hint_branches_for,
    hint_key,
    load_category_hints,
    merge_hints,
    persist_decision,
    tagged_at,
    write_hints,
)
from app.catalog.tree import Concept, ConceptTree, load_tree
from app.data.models import Dataset, DatasetDimension, Institution
from app.prompts.errors import PromptNotFoundError
from app.prompts.ref import PromptRef
from app.prompts.service import activate, add_version, get_active

logger = logging.getLogger("app.catalog.tag.jev")

PROMPT_DIR = Path(__file__).parent / "prompts"

#: Logical prompt name -> prompt key.
PROMPT_KEYS = {
    "branch": "catalog.tag.branch",
    "leaf": "catalog.tag.leaf",
    "category": "catalog.tag.category",
}
_DEFAULT_PROMPTS = (
    ("catalog.tag.branch", "tag_branch.md"),
    ("catalog.tag.leaf", "tag_leaf.md"),
    ("catalog.tag.category", "tag_category.md"),
)

PROGRESS_EVERY = 50


# --------------------------------------------------------------------------- #
# Prompt seeding
# --------------------------------------------------------------------------- #


def ensure_default_prompts(session: Session) -> None:
    """Seed the default bodies for keys with no active version (never overwrite)."""
    active: set[str] = set()
    for key, _filename in _DEFAULT_PROMPTS:
        try:
            get_active(session, key)
            active.add(key)
        except PromptNotFoundError:
            pass
    for key, filename in _DEFAULT_PROMPTS:
        if key in active:
            continue
        body = (PROMPT_DIR / filename).read_text(encoding="utf-8")
        version = add_version(session, key=key, body=body, note="Task 2.3 default")
        activate(session, key=key, version=version.version)
    session.flush()


def prompt_refs(session: Session) -> dict[str, PromptRef]:
    """The active prompt refs keyed by logical prompt name."""
    refs: dict[str, PromptRef] = {}
    for name, key in PROMPT_KEYS.items():
        view = get_active(session, key)
        refs[name] = PromptRef(key=view.key, version=view.version, checksum=view.checksum)
    return refs


# --------------------------------------------------------------------------- #
# Question + state builders (pure; unit-tested)
# --------------------------------------------------------------------------- #


def render_concept(body: str, concept: Concept) -> str:
    """Fill the ``{ad}``/``{tanim}``/``{degildir}`` placeholders of a prompt body."""
    return (
        body.replace("{ad}", concept.ad)
        .replace("{tanim}", concept.tanim)
        .replace("{degildir}", concept.degildir_text())
    )


def _noul(instructions: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {
            "evet": "Evet, bu veri seti bu konuya aittir.",
            "hayır": "Hayır, bu veri seti bu konuya ait değildir.",
        },
    }


def branch_question(body: str, concept: Concept) -> dict[str, Any]:
    """One branch ``noul`` question (the branch name is inside ``instructions``)."""
    return _noul(render_concept(body, concept))


def leaf_question(body: str, concept: Concept) -> dict[str, Any]:
    """One leaf ``noul`` question (the leaf name is inside ``instructions``)."""
    return _noul(render_concept(body, concept))


def category_question(body: str, concept: Concept) -> dict[str, Any]:
    """One category-hint branch question (no dataset name in the state)."""
    return _noul(render_concept(body, concept))


def branch_questions(tree: ConceptTree, body: str) -> dict[str, dict[str, Any]]:
    """All 24 branch questions, keyed by branch id, in tree order."""
    return {
        branch_id: branch_question(body, tree.branches[branch_id])
        for branch_id in tree.branch_order
    }


def leaf_questions(
    tree: ConceptTree, body: str, branches: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Leaf questions only for the passing ``branches``, keyed by leaf id."""
    questions: dict[str, dict[str, Any]] = {}
    for branch_id in branches:
        for leaf_id in tree.branch_leaves.get(branch_id, ()):
            questions[leaf_id] = leaf_question(body, tree.leaves[leaf_id])
    return questions


def chunk_questions(
    questions: Mapping[str, Any], max_size: int = MAX_QUESTIONS_PER_CALL
) -> list[dict[str, Any]]:
    """Split a question dict into chunks of at most ``max_size`` questions."""
    items = list(questions.items())
    size = max(1, max_size)
    return [dict(items[index : index + size]) for index in range(0, len(items), size)]


def build_tag_state(
    dataset: Dataset, view: rules.DatasetView, institution_code: str
) -> dict[str, Any]:
    """The dataset state sent to Jev (metadata only, never values)."""
    source_description = (dataset.attributes or {}).get("source_description")
    dimensions = [dimension.label for dimension in view.dimensions][:12]
    units = rules.dataset_units(view)
    return _clean_state(
        {
            "kurum": institution_code,
            "veri_seti": dataset.name,
            "kategori": category_text(dataset, institution_code),
            "frekans": ", ".join(rules.dataset_frequencies(view)),
            "birim": "; ".join(units[:5]),
            "kirilim_adlari": "; ".join(dimensions),
            "kaynak_aciklamasi": str(source_description)[:300] if source_description else "",
        }
    )


def build_category_state(institution_code: str, category: str) -> dict[str, Any]:
    """The hint state: institution + category only, no dataset name."""
    return _clean_state({"kurum": institution_code, "kategori": category})


def score_dataset(
    *,
    decider: JevDecider,
    tree: ConceptTree,
    dataset: Dataset,
    view: rules.DatasetView,
    institution_code: str,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
    branch_pass: float = BRANCH_PASS,
    leaf_accept: float = LEAF_ACCEPT,
    hints: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, float], dict[str, float], Decision]:
    """Run both Jev stages for one dataset and decide (no database).

    Returns ``(branch_scores, leaf_scores, decision)`` so persistence and tests
    share exactly the same scoring path as :class:`TagPass`.
    """
    state = build_tag_state(dataset, view, institution_code)
    branch_scores = ask_scores(
        decider, state, branch_questions(tree, bodies["branch"]), refs.get("branch")
    )
    leaf_scores: dict[str, float] = {}
    if not dataset.cok_konulu_derleme:
        passing = [
            branch_id
            for branch_id in tree.branch_order
            if branch_scores.get(branch_id, 0.0) >= branch_pass
        ]
        questions = leaf_questions(tree, bodies["leaf"], passing)
        if questions:
            leaf_scores = ask_scores(decider, state, questions, refs.get("leaf"))
    category = category_text(dataset, institution_code)
    hint = (
        hint_branches_for(hints, institution_code, category) if hints is not None else None
    )
    decision = decide_tags(
        branch_scores=branch_scores,
        leaf_scores=leaf_scores,
        cok_konulu=bool(dataset.cok_konulu_derleme),
        hint_branches=hint,
        branch_pass=branch_pass,
        leaf_accept=leaf_accept,
        tree=tree,
    )
    return branch_scores, leaf_scores, decision


# --------------------------------------------------------------------------- #
# Jev calls
# --------------------------------------------------------------------------- #


def ask_scores(
    decider: JevDecider,
    state: Any,
    questions: Mapping[str, Any],
    prompt_ref: PromptRef | None = None,
) -> dict[str, float]:
    """Ask every question (chunked) and return ``{question_id: yes_probability}``."""
    scores: dict[str, float] = {}
    for group in chunk_questions(questions):
        if not group:
            continue
        result = decider.decide(state, group, prompt_ref=prompt_ref)
        for question_id in group:
            answer = _answer_for(result, question_id)
            score, _confidence = linking.parse_score(answer)
            scores[question_id] = float(score)
    return scores


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #


@dataclass
class TagSummary:
    """Aggregated outcome of one tagging run."""

    tagged: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: int = 0
    aborted: bool = False
    error_messages: list[str] = field(default_factory=list)


class TagPass:
    """Scores the filtered catalog with Jev and writes ``dataset_tags`` rows."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        jev_factory: Callable[[], JevDecider] | None = None,
        tree: ConceptTree | None = None,
        workers: int = 4,
        limit: int | None = None,
        institution: str | None = None,
        dataset: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        branch_pass: float = BRANCH_PASS,
        leaf_accept: float = LEAF_ACCEPT,
        sample_identities: Sequence[str] | None = None,
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
        self.force = force
        self.branch_pass = branch_pass
        self.leaf_accept = leaf_accept
        self.sample_identities = list(sample_identities) if sample_identities is not None else None
        self.out = out
        self._decider: JevDecider | None = None
        self._decider_lock = threading.Lock()
        self._refs: dict[str, PromptRef] = {}
        self._bodies: dict[str, str] = {}
        self._hints: dict[str, dict[str, Any]] = {}
        self._row_counts: Counter = Counter()
        self._row_lock = threading.Lock()

    def _decider_for(self) -> JevDecider:
        if self._decider is None:
            with self._decider_lock:
                if self._decider is None:
                    self._decider = self._jev_factory()
        return self._decider

    def _load_prompts(self) -> None:
        with self.session_factory() as session:
            ensure_default_prompts(session)
            session.commit()
            for name, key in PROMPT_KEYS.items():
                view = get_active(session, key)
                self._refs[name] = PromptRef(
                    key=view.key, version=view.version, checksum=view.checksum
                )
                self._bodies[name] = view.body

    def run(self) -> TagSummary:
        self._load_prompts()
        self._hints = load_category_hints()
        with self.session_factory() as session:
            items = self._collect(session)
        if self.dry_run:
            for item in items:
                self.out(
                    f"dry-run dataset={item['identity']} cok_konulu={item['cok_konulu']}"
                )
            self.out(f"dry-run datasets={len(items)}")
            return TagSummary(skipped=len(items))

        progress = run_pool(
            items,
            self._worker(),
            step="tag",
            workers=self.workers,
            out=self.out,
            every=PROGRESS_EVERY,
        )
        for message in progress.error_messages:
            self.out(f"error {message}")
        summary = TagSummary(
            tagged=progress.done,
            accepted=progress.accepted,
            review=progress.review,
            rejected=self._row_counts.get("rejected", 0),
            errors=progress.errors,
            aborted=progress.aborted,
            error_messages=list(progress.error_messages),
        )
        return summary

    def _collect(self, session: Session) -> list[dict[str, Any]]:
        statement = (
            sa.select(
                Dataset.id,
                Institution.code,
                Dataset.external_code,
                Dataset.cok_konulu_derleme,
            )
            .join(Institution, Dataset.institution_id == Institution.id)
            .order_by(Dataset.id)
        )
        if self.dataset:
            statement = statement.where(Dataset.external_code == self.dataset)
        if self.institution:
            statement = statement.where(Institution.code == self.institution)
        if not self.force:
            statement = statement.where(Dataset.attributes["tagging"].astext.is_(None))
        wanted = set(self.sample_identities) if self.sample_identities is not None else None
        rows = session.execute(statement).all()
        items: list[dict[str, Any]] = []
        for dataset_id, institution_code, external_code, cok_konulu in rows:
            identity = f"{institution_code}:{external_code}"
            if wanted is not None and identity not in wanted:
                continue
            if has_manual_tags(session, dataset_id):
                continue
            items.append(
                {
                    "dataset_id": dataset_id,
                    "identity": identity,
                    "cok_konulu": bool(cok_konulu),
                }
            )
        if self.limit:
            items = items[: self.limit]
        return items

    def _worker(self) -> Callable[[dict[str, Any]], str | None]:
        def work(item: dict[str, Any]) -> str:
            decider = self._decider_for()
            with self.session_factory() as session:
                outcome, counts = self._process(session, decider, item["dataset_id"])
                session.commit()
            with self._row_lock:
                self._row_counts.update(counts)
            return outcome

        return work

    def _process(
        self, session: Session, decider: JevDecider, dataset_id: int
    ) -> tuple[str, Counter]:
        dataset, institution_code, institution_name = session.execute(
            sa.select(Dataset, Institution.code, Institution.name)
            .join(Institution, Dataset.institution_id == Institution.id)
            .where(Dataset.id == dataset_id)
            .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
        ).one()
        view = enrich._build_view(dataset, institution_code, institution_name)
        branch_scores, leaf_scores, decision = score_dataset(
            decider=decider,
            tree=self.tree,
            dataset=dataset,
            view=view,
            institution_code=institution_code,
            bodies=self._bodies,
            refs=self._refs,
            branch_pass=self.branch_pass,
            leaf_accept=self.leaf_accept,
            hints=self._hints,
        )
        prompt_versions = {
            name: {
                "key": ref.key,
                "version": ref.version,
                "checksum": ref.checksum,
            }
            for name, ref in self._refs.items()
        }
        persist_decision(
            session,
            dataset,
            decision,
            branch_scores=branch_scores,
            leaf_scores=leaf_scores,
            prompt_versions=prompt_versions,
            thresholds={"branch_pass": self.branch_pass, "leaf_accept": self.leaf_accept},
            tagged_at=tagged_at(),
            tree=self.tree,
        )
        counts: Counter = Counter(row.status for row in decision.rows)
        outcome = "review" if counts.get("review", 0) else "accepted"
        return outcome, counts


# --------------------------------------------------------------------------- #
# Category hints generation
# --------------------------------------------------------------------------- #


def generate_hints(
    session_factory: Callable[[], Any],
    *,
    jev_factory: Callable[[], JevDecider] | None = None,
    tree: ConceptTree | None = None,
    workers: int = 4,
    branch_pass: float = BRANCH_PASS,
    out: Callable[[str], None] = print,
) -> dict[str, dict[str, Any]]:
    """Ask the 24 branch questions per ``(institution, category)`` and write hints."""
    concept_tree = tree or load_tree()
    factory = jev_factory or default_jev_factory
    with session_factory() as session:
        ensure_default_prompts(session)
        session.commit()
        view = get_active(session, PROMPT_KEYS["category"])
        ref = PromptRef(key=view.key, version=view.version, checksum=view.checksum)
        body = view.body
        rows = session.execute(
            sa.select(Institution.code, Dataset).join(
                Dataset, Dataset.institution_id == Institution.id
            )
        ).all()

    categories: dict[tuple[str, str], None] = {}
    for institution_code, dataset in rows:
        categories.setdefault((institution_code, category_text(dataset, institution_code)), None)

    questions = {
        branch_id: category_question(body, concept_tree.branches[branch_id])
        for branch_id in concept_tree.branch_order
    }
    existing = load_category_hints()
    generated: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    decider = factory()

    def work(item: tuple[str, str]) -> str:
        institution_code, category = item
        scores = ask_scores(
            decider, build_category_state(institution_code, category), questions, ref
        )
        branches = {
            branch_id: score for branch_id, score in scores.items() if score >= branch_pass
        }
        ordered = dict(
            sorted(
                branches.items(),
                key=lambda pair: (-pair[1], concept_tree.branch_position.get(pair[0], 0)),
            )
        )
        with lock:
            generated[hint_key(institution_code, category)] = {
                "branches": ordered,
                "manual": False,
            }
        return "accepted"

    items = sorted(categories)
    progress = run_pool(
        items, work, step="hints", workers=workers, out=out, every=PROGRESS_EVERY
    )
    if progress.errors:
        out(f"hints errors={progress.errors}")

    merged = merge_hints(existing, generated)
    write_hints(HINTS_PATH, merged)
    return generated


__all__ = [
    "PROMPT_KEYS",
    "TagPass",
    "TagSummary",
    "ask_scores",
    "branch_question",
    "branch_questions",
    "build_category_state",
    "build_tag_state",
    "category_question",
    "chunk_questions",
    "ensure_default_prompts",
    "generate_hints",
    "leaf_question",
    "leaf_questions",
    "prompt_refs",
    "render_concept",
    "score_dataset",
]
