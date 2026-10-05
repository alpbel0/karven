"""Catalog series search (Task 2.4): plain-language request -> series recipes.

The flow follows ``DECISIONS.md`` §7 with the Task 2.4 decisions:

1. Jev scores the 24 concept-tree branches (1-5; p(4)+p(5) >= pass),
2. scores the leaves of the passing branches,
3. pure SQL picks the candidate datasets (accepted leaf tags, plus
   ``cok_konulu_derleme`` datasets with an accepted branch tag; ``revizyon_tablosu``
   is always excluded, ``arsiv`` is excluded on the first pass),
4. Jev re-scores the candidate datasets (chunked),
5. per dataset it chooses one code per non-time dimension (with EVREN rewrites
   when a choice is below the low-confidence threshold) and verifies each built
   series (``uyum`` + ``frekans``).

The result is a list of *recipes* (institution, dataset, codes, series name,
frequency, unit, measure info, rating); it never contains observation values and
never writes to the database.

CLI::

    python -m app.catalog.search "<request>" [--trace] [--json]

The agent tool is :func:`build_search_tool` (``find_series``); it is not wired
into the fetch agent.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any as _Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.catalog import enrich
from app.catalog import enrich_rules as rules
from app.catalog.enrich_jev import _answer_for, category_text
from app.catalog.jev_flow import (
    JevDecider,
    JevFlowError,
    Rating,
    _ask,
    noul_question,
    parse_rating,
    parse_score,
    score_question,
    select_dimension_code,
)
from app.catalog.tag import MAX_QUESTIONS_PER_CALL
from app.catalog.tag_jev import chunk_questions
from app.catalog.tree import ConceptTree, load_tree
from app.connectors.base import ROLE_TIME, build_series_definition
from app.data.errors import SeriesDefinitionError
from app.data.models import (
    Dataset,
    DatasetDimension,
    DatasetTag,
    Institution,
    Series,
)
from app.llm.chat import ChatClient
from app.llm.errors import LLMError
from app.llm.json_output import parse_json_output
from app.prompts.errors import PromptNotFoundError
from app.prompts.ref import PromptRef
from app.prompts.service import activate, add_version, get_active

logger = logging.getLogger("app.catalog.search")

PROMPT_DIR = Path(__file__).parent / "prompts"

#: Logical prompt name -> prompt key.
PROMPT_KEYS = {
    "branch": "catalog.search.branch",
    "leaf": "catalog.search.leaf",
    "dataset": "catalog.search.dataset",
    "dimension": "catalog.search.dimension",
    "verify": "catalog.search.verify",
    "rewrite": "catalog.search.rewrite",
}
_DEFAULT_PROMPTS = (
    ("catalog.search.branch", "search_branch.md"),
    ("catalog.search.leaf", "search_leaf.md"),
    ("catalog.search.dataset", "search_dataset.md"),
    ("catalog.search.dimension", "search_dimension.md"),
    ("catalog.search.verify", "search_verify.md"),
    ("catalog.search.rewrite", "search_rewrite.md"),
)

#: Live-prompt versioning note (Task 2.4 fix round).
#:
#: The live DB already has active ``catalog.search.*`` prompt versions (e.g.
#: ``catalog.search.rewrite`` v1). :func:`ensure_default_prompts` only seeds a
#: key that has NO active version, so editing one of the ``search_*.md`` files
#: below has NO effect on a live deployment. A changed default body requires
#: adding a new version and activating it with the prompt service
#: (``add_version`` + ``activate``, e.g. via ``python -m app.prompts``).
PROMPT_VERSION_NOTE = (
    "Changing a catalog.search.* default body requires adding and activating a "
    "new prompt version with the prompt service; ensure_default_prompts never "
    "overwrites an active version."
)

#: How many times an agent may call ``find_series`` in one run (decision 3).
SEARCH_TOOL_LIMIT = 4
#: A rewrite phrasing is kept when Jev says it still asks for the same data.
PHRASE_KEEP = 0.60
#: Appended to every dimension choice question (decision 9).
DIMENSION_SUFFIX = " İstek bu kırılımdan söz etmiyorsa toplamı/tümünü ifade eden seçeneği seçin."
#: Beam width for building code combinations (decision 5).
BEAM_WIDTH = 3
#: ``uyumsuzluk`` marker added to a near-miss candidate that is not strong.
NEAR_MISS_MISMATCH = "veri_seti_dusuk_uyum"

_STATUS_OK = "ok"
_STATUS_NO_MATCH = "no_strong_match"
_STATUS_FAILED = "search_failed"

_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"phrasings": {"type": "array", "items": {"type": "string"}}},
    "required": ["phrasings"],
}


# --------------------------------------------------------------------------- #
# Prompt seeding (same mechanism as tag_jev)
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
        version = add_version(session, key=key, body=body, note="Task 2.4 default")
        activate(session, key=key, version=version.version)
    session.flush()


def prompt_refs(session: Session) -> dict[str, PromptRef]:
    """The active prompt refs keyed by logical prompt name."""
    refs: dict[str, PromptRef] = {}
    for name, key in PROMPT_KEYS.items():
        view = get_active(session, key)
        refs[name] = PromptRef(key=view.key, version=view.version, checksum=view.checksum)
    return refs


def load_search_prompts(
    session: Session,
) -> tuple[dict[str, str], dict[str, PromptRef]]:
    """Read the active bodies and refs (read-only; seed with ``ensure_default_prompts``)."""
    bodies: dict[str, str] = {}
    refs: dict[str, PromptRef] = {}
    for name, key in PROMPT_KEYS.items():
        view = get_active(session, key)
        bodies[name] = view.body
        refs[name] = PromptRef(key=view.key, version=view.version, checksum=view.checksum)
    return bodies, refs


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #


def _render(body: str, **variables: object) -> str:
    text = body
    for key, value in variables.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _ordered_non_time(dataset: Dataset) -> list[DatasetDimension]:
    return [
        dimension
        for dimension in sorted(dataset.dimensions, key=lambda row: row.position)
        if dimension.role != ROLE_TIME
    ]


def _active_dimension_codes(dimension: DatasetDimension) -> list[_Any]:
    return [code for code in dimension.codes if not (code.attributes or {}).get("removed_at")]


def _dataset_text(dataset: Dataset, view: rules.DatasetView, institution_code: str) -> str:
    """A metadata-only dataset description for the dataset score question."""
    dimensions = [dimension.label for dimension in view.dimensions][:12]
    source_description = (dataset.attributes or {}).get("source_description")
    parts = [f"{dataset.name} [{dataset.external_code}]"]
    fields = {
        "kurum": institution_code,
        "kategori": category_text(dataset, institution_code),
        "frekans": ", ".join(rules.dataset_frequencies(view)),
        "birim": ", ".join(rules.dataset_units(view)[:5]),
        "kirilimlar": ", ".join(dimensions),
    }
    parts.extend(f"{name}: {value}" for name, value in fields.items() if value)
    if source_description:
        parts.append(f"kaynak aciklamasi: {str(source_description)[:300]}")
    return " | ".join(parts)


def _ask_ratings(
    jev: JevDecider,
    state: _Any,
    questions: Mapping[str, dict[str, _Any]],
    prompt_ref: PromptRef | None,
    *,
    chunk_size: int = MAX_QUESTIONS_PER_CALL,
) -> dict[str, Rating]:
    """Ask every score question (chunked) and parse each answer into a Rating."""
    ratings: dict[str, Rating] = {}
    for group in chunk_questions(questions, chunk_size):
        if not group:
            continue
        result = jev.decide(state, group, prompt_ref=prompt_ref)
        for question_id in group:
            ratings[question_id] = parse_rating(_answer_for(result, question_id))
    return ratings


def _beam_combinations(
    options_by_dimension: Sequence[tuple[str, Sequence[tuple[str, float, str]]]],
    width: int = BEAM_WIDTH,
) -> list[tuple[dict[str, str], float]]:
    """Build up to ``width`` full code combinations ranked by product confidence."""
    beam: list[tuple[dict[str, str], float]] = [({}, 1.0)]
    for dimension_code, options in options_by_dimension:
        expanded: list[tuple[dict[str, str], float]] = []
        for codes, score in beam:
            for code, confidence, _label in options:
                combined = dict(codes)
                combined[dimension_code] = code
                expanded.append((combined, score * confidence))
        expanded.sort(key=lambda item: -item[1])
        beam = expanded[:width]
    return beam


# --------------------------------------------------------------------------- #
# Candidate datasets (pure SQL)
# --------------------------------------------------------------------------- #


def _load_candidate_datasets(
    session: Session,
    *,
    leaf_ids: Sequence[str],
    branch_ids: Sequence[str],
    arsiv: str = "exclude",
) -> list[tuple[Dataset, str, str]]:
    """Candidate datasets as ``(dataset, institution_code, institution_name)``.

    Leaf-tagged datasets plus ``cok_konulu_derleme`` datasets branch-tagged with a
    passing branch. ``revizyon_tablosu`` is always excluded; ``arsiv`` is excluded
    (``arsiv='exclude'``), restricted to archived rows (``arsiv='only'``) or left
    in (``arsiv='include'``).
    """
    subqueries: list[sa.Select[tuple[int]]] = []
    if leaf_ids:
        subqueries.append(
            sa.select(DatasetTag.dataset_id).where(
                DatasetTag.status == "accepted",
                DatasetTag.level == "leaf",
                DatasetTag.tag_id.in_(list(leaf_ids)),
            )
        )
    if branch_ids:
        subqueries.append(
            sa.select(DatasetTag.dataset_id)
            .join(Dataset, DatasetTag.dataset_id == Dataset.id)
            .where(
                Dataset.cok_konulu_derleme.is_(True),
                DatasetTag.status == "accepted",
                DatasetTag.level == "branch",
                DatasetTag.tag_id.in_(list(branch_ids)),
            )
        )
    if not subqueries:
        return []
    union_query = subqueries[0]
    if len(subqueries) > 1:
        union_query = union_query.union(subqueries[1])
    candidate_ids = union_query.subquery()

    statement = (
        sa.select(Dataset, Institution.code, Institution.name)
        .join(Institution, Dataset.institution_id == Institution.id)
        .where(Dataset.id.in_(sa.select(candidate_ids.c.dataset_id)))
        .where(Dataset.revizyon_tablosu.is_(False))
        .where(Dataset.veri_yok.is_(False))
        .options(
            selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes),
            selectinload(Dataset.measure_combinations),
        )
        .order_by(Dataset.id)
    )
    if arsiv == "exclude":
        statement = statement.where(Dataset.arsiv.is_(False))
    elif arsiv == "only":
        statement = statement.where(Dataset.arsiv.is_(True))
    return list(session.execute(statement).all())


# --------------------------------------------------------------------------- #
# Rewrites (decision 7)
# --------------------------------------------------------------------------- #


def _rewrite_phrasings(
    chat: ChatClient,
    request: str,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
) -> tuple[list[str], str | None]:
    """Ask EVREN for alternative phrasings; ``([], note)`` when unavailable.

    The rewrite now depends only on the request (fix round decision 2): the
    prompt asks for meaning-preserving rewordings, never a dimension focus. The
    ``{boyut}`` placeholder is gone; :func:`_render` tolerates its absence.
    """
    body = _render(bodies["rewrite"], istek=request)
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "search_rewrite", "schema": _REWRITE_SCHEMA},
    }
    try:
        result = chat.complete(
            [{"role": "user", "content": body}],
            response_format=response_format,
            prompt_ref=refs.get("rewrite"),
        )
        data = parse_json_output(result.content or "")
        if not isinstance(data, dict):
            raise JevFlowError(f"rewrite answer is not an object: {data!r}")
        phrasings = [str(item) for item in (data.get("phrasings") or []) if str(item).strip()]
    except (LLMError, JevFlowError) as exc:
        logger.warning("search rewrite unavailable for %r: %s", request, exc)
        return [], "rewrite_unavailable"
    phrasings = phrasings[:2]
    if len(phrasings) < 2:
        logger.warning("search rewrite returned fewer than 2 phrasings for %r", request)
        return [], "rewrite_unavailable"
    return phrasings, None


def _keep_phrasings(
    jev: JevDecider,
    request: str,
    phrasings: Sequence[str],
    prompt_ref: PromptRef | None,
) -> list[str]:
    """Keep only the phrasings Jev says still ask for the same data."""
    kept: list[str] = []
    for phrasing in phrasings:
        instructions = (
            f"Yeniden yazım: '{phrasing}'. Özgün istek: '{request}'. "
            "Bu yeniden yazım özgün istekle aynı veriyi mi istiyor? "
            "Anlam, sayı ve yer adları korunmuş mu?"
        )
        answer = _ask(
            jev,
            {"istek": request},
            noul_question(
                instructions,
                {"evet": "Aynı veriyi istiyor", "hayır": "Farklı bir şey istiyor"},
            ),
            prompt_ref=prompt_ref,
        )
        score, _confidence = parse_score(answer)
        if score >= PHRASE_KEEP:
            kept.append(phrasing)
    return kept


class _RewriteCache:
    """Per-search EVREN rewrite + Jev drift check, computed once and reused.

    Both the rewrite and the drift check depend only on the request, so one EVREN
    call and one Jev drift question per phrasing serve every dimension and dataset
    in a search. The cache records the unavailable note in the trace exactly once.
    """

    def __init__(
        self,
        *,
        chat: ChatClient,
        request: str,
        bodies: Mapping[str, str],
        refs: Mapping[str, PromptRef | None],
        trace: dict[str, _Any],
    ) -> None:
        self._chat = chat
        self._request = request
        self._bodies = bodies
        self._refs = refs
        self._trace = trace
        self._resolved = False
        self._phrasings: list[str] = []
        self._kept: list[str] = []
        self.note: str | None = None

    def resolve(self, jev: JevDecider) -> tuple[list[str], list[str]]:
        """Return ``(phrasings, kept)``, computing them on first use."""
        if self._resolved:
            return self._phrasings, self._kept
        self._resolved = True
        phrasings, note = _rewrite_phrasings(self._chat, self._request, self._bodies, self._refs)
        self.note = note
        self._phrasings = phrasings
        if phrasings:
            self._kept = _keep_phrasings(jev, self._request, phrasings, self._refs.get("dimension"))
        self._trace.setdefault("rewrites", []).append(
            {
                "request": self._request,
                "phrasings": list(self._phrasings),
                "kept": list(self._kept),
                "note": self.note,
            }
        )
        return self._phrasings, self._kept


def _choose_dimension_codes(
    jev: JevDecider,
    request: str,
    dimension: DatasetDimension,
    *,
    cache: _RewriteCache,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
    settings: _Any,
) -> tuple[list[tuple[str, float, str]], bool]:
    """One or two ``(code, confidence, label)`` options for one dimension.

    A dimension with exactly one active code is chosen without any Jev call
    (confidence 1.0, flagged ``single_code`` in the trace). Otherwise a confident
    answer stands alone, and a low-confidence answer triggers the cached EVREN
    rewrites: agreeing answers collapse to one code, disagreeing answers keep up
    to two ranked candidates.
    """
    active = _active_dimension_codes(dimension)
    if len(active) == 1:
        only = active[0]
        return [(str(only.code), 1.0, str(only.label))], True

    prompt_ref = refs.get("dimension")
    code, confidence, label = select_dimension_code(
        jev,
        {"istek": request},
        request,
        dimension,
        prompt_ref=prompt_ref,
        instruction_suffix=DIMENSION_SUFFIX,
        instruction_template=bodies["dimension"],
        allow_stop=True,
    )
    results: list[tuple[str, float, str]] = [(str(code), confidence, str(label))]
    if confidence < settings.search_low_confidence:
        _phrasings, kept = cache.resolve(jev)
        for phrasing in kept:
            p_code, p_conf, p_label = select_dimension_code(
                jev,
                {"istek": phrasing},
                phrasing,
                dimension,
                prompt_ref=prompt_ref,
                instruction_suffix=DIMENSION_SUFFIX,
                instruction_template=bodies["dimension"],
                allow_stop=True,
            )
            results.append((str(p_code), p_conf, str(p_label)))

    codes_seen = {item[0] for item in results}
    if len(codes_seen) == 1:
        chosen = results[0][0]
        min_confidence = min(item[1] for item in results)
        chosen_label = next(item[2] for item in results if item[0] == chosen)
        return [(chosen, min_confidence, chosen_label)], False

    votes: dict[str, list[float]] = {}
    labels: dict[str, str] = {}
    for item_code, item_confidence, item_label in results:
        votes.setdefault(item_code, []).append(item_confidence)
        labels.setdefault(item_code, item_label)
    ranked = sorted(
        votes.items(),
        key=lambda pair: (-len(pair[1]), -(sum(pair[1]) / len(pair[1]))),
    )
    return [
        (item_code, sum(confidences) / len(confidences), labels[item_code])
        for item_code, confidences in ranked[:2]
    ], False


# --------------------------------------------------------------------------- #
# Measure info + series verification
# --------------------------------------------------------------------------- #


def measure_info(dataset: Dataset, codes: Mapping[str, str]) -> dict[str, _Any] | None:
    """The measure fields of the combination whose codes are contained in ``codes``."""
    combinations = sorted(
        dataset.measure_combinations, key=lambda row: row.id if row.id is not None else 0
    )
    for combination in combinations:
        combination_codes = dict(combination.codes or {})
        if all(code in codes and codes[code] == value for code, value in combination_codes.items()):
            return {
                "measure_type": combination.measure_type,
                "data_nature": combination.data_nature,
                "aggregation": combination.aggregation,
                "para_birimi": combination.para_birimi,
                "nominal_mi": combination.nominal_mi,
                "mevsim_arindirilmis": combination.mevsim_arindirilmis,
                "kumulatif": bool(combination.kumulatif),
            }
    return None


def _series_id(session: Session, dataset: Dataset, external_code: str) -> int | None:
    """The existing ``series`` row id for a recipe, or ``None`` (never creates one)."""
    return session.scalar(
        sa.select(Series.id).where(
            Series.institution_id == dataset.institution_id,
            Series.external_code == external_code,
        )
    )


def _verify_item(
    session: Session,
    dataset: Dataset,
    institution_code: str,
    codes: dict[str, str],
    definition: _Any,
    request: str,
    *,
    jev: JevDecider,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
    settings: _Any,
) -> tuple[dict[str, _Any], bool]:
    """Build one recipe and whether it is a strong (4-5 + frequency) match."""
    uyum_question = score_question(_render(bodies["verify"], series=definition.name, istek=request))
    frekans_question = noul_question(
        f"'{definition.name}' serisinin frekansı ({definition.frequency}) "
        f"'{request}' isteğiyle uyumlu mu? İstekte frekans belirtilmemişse evet de.",
        {"evet": "Frekans istekle uyumlu", "hayır": "Frekans istekle uyumsuz"},
    )
    result = jev.decide(
        {"istek": request},
        {"uyum": uyum_question, "frekans": frekans_question},
        prompt_ref=refs.get("verify"),
    )
    uyum = parse_rating(_answer_for(result, "uyum"))
    frekans_yes, _frekans_confidence = parse_score(_answer_for(result, "frekans"))

    uyumsuzluk: list[str] = []
    if not uyum.passes(settings.search_pass_probability):
        uyumsuzluk.append("genel_uyum_dusuk")
    if frekans_yes < settings.search_frequency_floor:
        uyumsuzluk.append("frekans")

    item = {
        "institution": institution_code,
        "dataset": dataset.external_code,
        "dataset_name": dataset.name,
        "codes": dict(codes),
        "series_external_code": definition.external_code,
        "series_name": definition.name,
        "frequency": definition.frequency,
        "unit": definition.unit,
        "measure": measure_info(dataset, codes),
        "rating": {"p_high": uyum.p_high, "rating_15": uyum.rating_15},
        "arsiv": bool(dataset.arsiv),
        "uyumsuzluk": uyumsuzluk,
        "series_id": _series_id(session, dataset, definition.external_code),
    }
    return item, not uyumsuzluk


def _build_dataset_series(
    session: Session,
    dataset: Dataset,
    institution_code: str,
    request: str,
    *,
    jev: JevDecider,
    cache: _RewriteCache,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
    settings: _Any,
    trace: dict[str, _Any],
) -> tuple[list[dict[str, _Any]], list[dict[str, _Any]]]:
    """Steps 5a-5c for one dataset: choose codes, build, verify."""
    options_by_dimension: list[tuple[str, list[tuple[str, float, str]]]] = []
    chosen_trace: dict[str, list[dict[str, _Any]]] = {}
    for dimension in _ordered_non_time(dataset):
        options, single_code = _choose_dimension_codes(
            jev,
            request,
            dimension,
            cache=cache,
            bodies=bodies,
            refs=refs,
            settings=settings,
        )
        options_by_dimension.append((dimension.code, options))
        entry = [
            {"code": code, "confidence": round(confidence, 4)} for code, confidence, _ in options
        ]
        if single_code:
            entry[0]["single_code"] = True
        chosen_trace[dimension.code] = entry
    trace.setdefault("chosen", {})[dataset.external_code] = chosen_trace

    strong: list[dict[str, _Any]] = []
    candidates: list[dict[str, _Any]] = []
    for codes, _product in _beam_combinations(options_by_dimension):
        try:
            definition = build_series_definition(dataset, codes)
        except SeriesDefinitionError as exc:
            logger.error(
                "series definition failed for dataset %s codes=%s: %s",
                dataset.external_code,
                codes,
                exc,
            )
            trace.setdefault("series_errors", []).append(
                {"dataset": dataset.external_code, "codes": dict(codes), "error": str(exc)}
            )
            continue
        item, is_strong = _verify_item(
            session,
            dataset,
            institution_code,
            codes,
            definition,
            request,
            jev=jev,
            bodies=bodies,
            refs=refs,
            settings=settings,
        )
        (strong if is_strong else candidates).append(item)
    return strong, candidates


def _rate_datasets(
    jev: JevDecider,
    datasets: Sequence[tuple[Dataset, str, str]],
    request: str,
    *,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
    settings: _Any,
    trace: dict[str, _Any],
) -> tuple[
    list[tuple[Dataset, str, str, Rating]],
    list[tuple[Dataset, str, str, Rating]],
]:
    """Step 4: score every candidate dataset.

    Returns ``(passing, near_miss)``. ``passing`` is the strong-rating list, capped
    and best first. ``near_miss`` holds the datasets that did not pass but clear
    ``search_near_miss_probability`` (uncapped here; the caller sorts and caps the
    combined pool after both passes).
    """
    questions: dict[str, dict[str, _Any]] = {}
    for dataset, institution_code, institution_name in datasets:
        view = enrich._build_view(dataset, institution_code, institution_name)
        questions[f"ds-{dataset.id}"] = score_question(
            _render(bodies["dataset"], dataset=_dataset_text(dataset, view, institution_code))
        )
    ratings = _ask_ratings(
        jev,
        {"istek": request},
        questions,
        refs.get("dataset"),
        chunk_size=settings.search_dataset_chunk,
    )
    passed: list[tuple[Dataset, str, str, Rating]] = []
    near_miss: list[tuple[Dataset, str, str, Rating]] = []
    for dataset, institution_code, institution_name in datasets:
        rating = ratings[f"ds-{dataset.id}"]
        is_pass = rating.passes(settings.search_pass_probability)
        trace.setdefault("datasets", []).append(
            {
                "dataset": dataset.external_code,
                "dataset_id": dataset.id,
                "p_high": rating.p_high,
                "rating_15": rating.rating_15,
                "pass": is_pass,
            }
        )
        row = (dataset, institution_code, institution_name, rating)
        if is_pass:
            passed.append(row)
        elif rating.p_high >= settings.search_near_miss_probability:
            near_miss.append(row)
    passed.sort(key=lambda item: item[3].rating_15, reverse=True)
    near_miss.sort(key=lambda item: item[3].rating_15, reverse=True)
    return passed[: settings.search_max_datasets], near_miss


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #


def _result(
    status: str,
    strong: Sequence[dict[str, _Any]],
    candidates: Sequence[dict[str, _Any]],
    searched_archive: bool,
    message: str,
) -> dict[str, _Any]:
    return {
        "status": status,
        "strong": list(strong),
        "candidates": list(candidates),
        "searched_archive": searched_archive,
        "message": message,
    }


def _finalize(
    strong: list[dict[str, _Any]],
    candidates: list[dict[str, _Any]],
    searched_archive: bool,
    settings: _Any,
) -> dict[str, _Any]:
    strong.sort(key=lambda item: item["rating"]["rating_15"], reverse=True)
    candidates.sort(key=lambda item: item["rating"]["rating_15"], reverse=True)
    strong = strong[: settings.search_max_results]
    candidates = candidates[: max(0, settings.search_max_results - len(strong))]
    if strong:
        return _result(
            _STATUS_OK,
            strong,
            candidates,
            searched_archive,
            f"{len(strong)} güçlü eşleşme bulundu.",
        )
    if candidates:
        message = (
            "Hiçbir seri 4-5 puan almadı; liste isteğe en yakın adayları ve "
            "uyumsuzluklarını gösterir."
        )
    else:
        message = "İsteği karşılayan veri seti/seri bulunamadı."
    return _result(_STATUS_NO_MATCH, strong, candidates, searched_archive, message)


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #


def _run_search(
    session: Session,
    request: str,
    *,
    jev: JevDecider,
    chat: ChatClient,
    tree: ConceptTree,
    bodies: Mapping[str, str],
    refs: Mapping[str, PromptRef | None],
    settings: _Any,
    trace: dict[str, _Any],
) -> dict[str, _Any]:
    state = {"istek": request}

    branch_questions = {
        branch_id: score_question(
            _render(
                bodies["branch"],
                ad=tree.branches[branch_id].ad,
                tanim=tree.branches[branch_id].tanim,
                degildir=tree.branches[branch_id].degildir_text(),
            )
        )
        for branch_id in tree.branch_order
    }
    branch_ratings = _ask_ratings(jev, state, branch_questions, refs.get("branch"))
    trace["branches"] = {
        branch_id: {
            "p_high": branch_ratings[branch_id].p_high,
            "rating_15": branch_ratings[branch_id].rating_15,
            "pass": branch_ratings[branch_id].passes(settings.search_pass_probability),
        }
        for branch_id in tree.branch_order
    }
    passing_branches = [
        branch_id
        for branch_id in tree.branch_order
        if branch_ratings[branch_id].passes(settings.search_pass_probability)
    ]
    if not passing_branches:
        return _finalize([], [], False, settings)

    leaf_questions: dict[str, dict[str, _Any]] = {}
    for branch_id in passing_branches:
        for leaf_id in tree.branch_leaves.get(branch_id, ()):
            leaf_questions[leaf_id] = score_question(
                _render(
                    bodies["leaf"],
                    ad=tree.leaves[leaf_id].ad,
                    tanim=tree.leaves[leaf_id].tanim,
                    degildir=tree.leaves[leaf_id].degildir_text(),
                )
            )
    leaf_ratings = _ask_ratings(jev, state, leaf_questions, refs.get("leaf"))
    trace["leaves"] = {
        leaf_id: {
            "p_high": leaf_ratings[leaf_id].p_high,
            "rating_15": leaf_ratings[leaf_id].rating_15,
            "pass": leaf_ratings[leaf_id].passes(settings.search_pass_probability),
        }
        for leaf_id in leaf_questions
    }
    passing_leaves = [
        leaf_id
        for leaf_id in leaf_questions
        if leaf_ratings[leaf_id].passes(settings.search_pass_probability)
    ]

    cache = _RewriteCache(chat=chat, request=request, bodies=bodies, refs=refs, trace=trace)
    strong: list[dict[str, _Any]] = []
    candidates: list[dict[str, _Any]] = []
    tried: set[int] = set()
    near_miss_pool: list[tuple[Dataset, str, str, Rating]] = []

    def run_pass(arsiv: str) -> bool:
        """Process one candidate pass; return whether it had candidates to rate."""
        datasets = _load_candidate_datasets(
            session, leaf_ids=passing_leaves, branch_ids=passing_branches, arsiv=arsiv
        )
        datasets = [row for row in datasets if row[0].id not in tried]
        trace.setdefault("passes", []).append({"arsiv": arsiv, "candidate_count": len(datasets)})
        if not datasets:
            return False
        passing, near_miss = _rate_datasets(
            jev, datasets, request, bodies=bodies, refs=refs, settings=settings, trace=trace
        )
        near_miss_pool.extend(near_miss)
        for dataset, institution_code, _name, _rating in passing:
            tried.add(dataset.id)
            pass_strong, pass_candidates = _build_dataset_series(
                session,
                dataset,
                institution_code,
                request,
                jev=jev,
                cache=cache,
                bodies=bodies,
                refs=refs,
                settings=settings,
                trace=trace,
            )
            strong.extend(pass_strong)
            candidates.extend(pass_candidates)
            if len(strong) >= settings.search_max_results:
                break
        return True

    run_pass("exclude")
    searched_archive = False
    if not strong:
        searched_archive = run_pass("only")

    if not strong:
        near_miss_pool.sort(key=lambda item: item[3].rating_15, reverse=True)
        near_miss = [row for row in near_miss_pool if row[0].id not in tried][
            : settings.search_near_miss_datasets
        ]
        trace["near_miss"] = [
            {
                "dataset": dataset.external_code,
                "p_high": rating.p_high,
                "rating_15": rating.rating_15,
            }
            for dataset, _code, _name, rating in near_miss
        ]
        for dataset, institution_code, _name, _rating in near_miss:
            tried.add(dataset.id)
            near_strong, near_candidates = _build_dataset_series(
                session,
                dataset,
                institution_code,
                request,
                jev=jev,
                cache=cache,
                bodies=bodies,
                refs=refs,
                settings=settings,
                trace=trace,
            )
            strong.extend(near_strong)
            for item in near_candidates:
                item["uyumsuzluk"].append(NEAR_MISS_MISMATCH)
            candidates.extend(near_candidates)
            if len(strong) >= settings.search_max_results:
                break

    return _finalize(strong, candidates, searched_archive, settings)


def search_series(
    session: Session,
    request: str,
    *,
    jev: JevDecider,
    chat: ChatClient,
    tree: ConceptTree | None = None,
    bodies: Mapping[str, str] | None = None,
    refs: Mapping[str, PromptRef | None] | None = None,
    settings: _Any = None,
    include_trace: bool = False,
) -> dict[str, _Any]:
    """Search the catalog for up to three series recipes matching ``request``.

    Read-only: the function never adds, flushes or commits anything. Any
    ``LLMError``/``JevFlowError`` escaping a Jev call aborts the search and is
    returned as ``search_failed`` with ``budget_refund`` (never as "no match").
    """
    if settings is None:
        from app.config import settings as default_settings

        settings = default_settings
    concept_tree = tree or load_tree()
    if bodies is None or refs is None:
        bodies, refs = load_search_prompts(session)

    trace: dict[str, _Any] = {"request": request}
    try:
        result = _run_search(
            session,
            request,
            jev=jev,
            chat=chat,
            tree=concept_tree,
            bodies=bodies,
            refs=refs,
            settings=settings,
            trace=trace,
        )
    except (LLMError, JevFlowError) as exc:
        logger.error("search failed for %r: %s", request, exc)
        trace["error"] = f"{type(exc).__name__}: {exc}"
        failed = {
            "status": _STATUS_FAILED,
            "reason": str(exc),
            "budget_refund": True,
            "strong": [],
            "candidates": [],
            "searched_archive": False,
            "message": f"Arama başarısız oldu: {exc}",
        }
        if include_trace:
            failed["trace"] = trace
        return failed
    if include_trace:
        result["trace"] = trace
    return result


# --------------------------------------------------------------------------- #
# Agent tool
# --------------------------------------------------------------------------- #


def _schema(description: str, properties: dict[str, _Any], required: list[str]) -> dict[str, _Any]:
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "required": required,
    }


def build_search_tool(
    session_factory: _Any,
    jev: JevDecider,
    chat: ChatClient,
    *,
    settings: _Any = None,
) -> dict[str, tuple[dict[str, _Any], _Any]]:
    """The ``find_series`` agent tool (``{name: (schema, function)}``)."""
    if settings is None:
        from app.config import settings as default_settings

        settings = default_settings

    def tool_find_series(request: str) -> dict[str, _Any]:
        with session_factory() as session:
            ensure_default_prompts(session)
            session.commit()
            bodies, refs = load_search_prompts(session)
            return search_series(
                session,
                request,
                jev=jev,
                chat=chat,
                bodies=bodies,
                refs=refs,
                settings=settings,
            )

    schema = _schema(
        "Catalog series search. Give a plain-language Turkish data request and "
        "get up to 3 series recipes (institution, dataset, codes, name, "
        "frequency, unit, measure) with no values. Use at most 4 searches per "
        "run, refine the request instead of repeating it.",
        {"request": {"type": "string"}},
        ["request"],
    )
    return {"find_series": (schema, tool_find_series)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.search",
        description="Search the catalog for series recipes with Jev + EVREN (Task 2.4).",
    )
    parser.add_argument("request", help="plain-language Turkish data request")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="include the branch/leaf/dataset/rewrite trace in the result",
    )
    parser.add_argument("--json", action="store_true", help="print the raw JSON result")
    return parser


def _print_readable(result: Mapping[str, _Any]) -> None:
    print(f"status: {result.get('status')}")
    print(f"message: {result.get('message')}")
    if result.get("searched_archive"):
        print("searched_archive: true")
    for label in ("strong", "candidates"):
        items = result.get(label) or []
        if not items:
            continue
        print(f"{label}:")
        for item in items:
            print(
                f"  {item['institution']}:{item['dataset']} "
                f"{item['series_external_code']}\t{item['series_name']}\t"
                f"frekans={item['frequency']}\tbirim={item['unit']}\t"
                f"p4p5={item['rating']['p_high']:.2f}\t"
                f"uyumsuzluk={','.join(item['uyumsuzluk']) or '-'}\tarsiv={item['arsiv']}"
            )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)

    from app.config import settings
    from app.db.session import SessionLocal
    from app.llm.jev import JevClient

    jev = JevClient(settings)
    chat = ChatClient(settings)
    try:
        with SessionLocal() as session:
            ensure_default_prompts(session)
            session.commit()
            bodies, refs = load_search_prompts(session)
            result = search_series(
                session,
                args.request,
                jev=jev,
                chat=chat,
                bodies=bodies,
                refs=refs,
                settings=settings,
                include_trace=bool(args.trace),
            )
    finally:
        jev.close()
        chat.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_readable(result)
        if args.trace:
            print("trace:")
            print(json.dumps(result.get("trace"), ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("status") == _STATUS_FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BEAM_WIDTH",
    "DIMENSION_SUFFIX",
    "NEAR_MISS_MISMATCH",
    "PHRASE_KEEP",
    "PROMPT_KEYS",
    "PROMPT_VERSION_NOTE",
    "SEARCH_TOOL_LIMIT",
    "build_parser",
    "build_search_tool",
    "ensure_default_prompts",
    "load_search_prompts",
    "measure_info",
    "main",
    "prompt_refs",
    "search_series",
]
