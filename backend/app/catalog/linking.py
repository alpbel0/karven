"""Catalog linking (Turcat -> databrowser2), reusable by the CİP task.

Jev proposes a mapping between one ``from`` breakdown (a Turcat indicator) and
one ``to`` breakdown (a databrowser2 series). The search follows DECISIONS §7:

1. choose candidate target datasets hierarchically (category first, then name),
2. choose each non-time dimension code hierarchically (parent level first),
3. verify the chosen series against the request with a final ``noul`` question.

Every question carries at most 255 options; longer lists are narrowed with
extra choice questions. A proposal keeps the top candidate only and is stored
with ``status='proposed'`` — nothing is auto-accepted.

CLI::

    python -m app.catalog.linking propose --from-dataset TURCAT_REEL [--limit N] [--dry-run]
    python -m app.catalog.linking list --status proposed
    python -m app.catalog.linking show ID
    python -m app.catalog.linking set-mapping ID --json '{"scale": "1000"}'
    python -m app.catalog.linking accept ID
    python -m app.catalog.linking reject ID [--note "..."]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.catalog.jev_flow import (
    MAX_CHOICE_OPTIONS,
    ChoiceAnswer,
    JevDecider,
    JevFlowError,
    _ask,
    choice_question,
    narrow_choice,
    noul_question,
    parse_choice,
    parse_score,
    select_dimension_code,
)
from app.catalog.mapping import (
    MappingError,
    mapping_from_json,
    mapping_summary,
    parse_mapping,
    set_link_mapping,
    to_storage,
)
from app.data.models import CatalogLink, Dataset, DatasetDimension, Institution

logger = logging.getLogger("app.catalog.linking")

#: Backwards-compatible alias: every ``except LinkingError`` still catches the
#: shared flow error raised by :mod:`app.catalog.jev_flow`.
LinkingError = JevFlowError

SAME_SERIES_THRESHOLD = 0.75
RELATED_THRESHOLD = 0.35

RELATION_SAME_SERIES = "same_series"
RELATION_RELATED = "related"
METHOD_JEV = "jev_proposed"
METHOD_MANUAL = "manual"
STATUS_PROPOSED = "proposed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

INDICATOR_DIMENSION = "INDICATOR"


@dataclass(frozen=True)
class LinkProposal:
    """One proposed (or skipped) link, for reporting and persistence."""

    from_code: str
    from_name: str
    to_dataset_id: int | None
    to_dataset_code: str | None
    to_codes: dict[str, str] = field(default_factory=dict)
    to_name: str | None = None
    relation: str | None = None
    confidence: float | None = None
    status: str = STATUS_PROPOSED
    note: str | None = None
    persisted: bool = False


def _dataset_label(dataset: Dataset) -> str:
    return f"{dataset.name} [{dataset.external_code}]"


def select_target_dataset(
    jev: JevDecider,
    request: str,
    datasets: Sequence[Dataset],
    *,
    prompt_ref: Any = None,
) -> tuple[Dataset, float]:
    """Choose a target dataset: top-level category first, then dataset name."""
    if not datasets:
        raise LinkingError("no target datasets available")
    state = {"request": request}
    groups: dict[str, list[Dataset]] = {}
    for dataset in datasets:
        category = (dataset.source_category or "(kategorisiz)").split(" / ")[0]
        groups.setdefault(category, []).append(dataset)
    if len(groups) > 1:
        categories = sorted(groups)
        chosen, _ = narrow_choice(
            jev,
            state,
            f"'{request}' isteğine hangi veri kategorisi en uygun?",
            categories,
            labeler=str,
            prompt_ref=prompt_ref,
        )
        candidates = groups[chosen]
    else:
        candidates = next(iter(groups.values()))
    return narrow_choice(
        jev,
        state,
        f"'{request}' isteğine en uygun veri seti hangisi?",
        candidates,
        labeler=_dataset_label,
        prompt_ref=prompt_ref,
    )


def _ordered_dimensions(dataset: Dataset) -> list[DatasetDimension]:
    return [
        dimension
        for dimension in sorted(dataset.dimensions, key=lambda dim: dim.position)
        if dimension.role != "time"
    ]


def select_target_codes(
    jev: JevDecider,
    request: str,
    dataset: Dataset,
    *,
    prompt_ref: Any = None,
) -> tuple[dict[str, str], float]:
    """Choose one code per non-time dimension of ``dataset``."""
    state = {"request": request}
    codes: dict[str, str] = {}
    confidences: list[float] = []
    for dimension in _ordered_dimensions(dataset):
        code, confidence, _label = select_dimension_code(
            jev, state, request, dimension, prompt_ref=prompt_ref
        )
        codes[dimension.code] = code
        confidences.append(confidence)
    return codes, min(confidences) if confidences else 1.0


def verify_match(
    jev: JevDecider,
    request: str,
    series_name: str,
    *,
    prompt_ref: Any = None,
) -> tuple[float, float]:
    """Final ``noul`` check: does the chosen series match the request?"""
    answer = _ask(
        jev,
        {"request": request},
        noul_question(
            f"'{series_name}' serisi '{request}' isteğiyle aynı gösterge mi?",
            {"evet": "aynı gösterge", "hayır": "farklı gösterge"},
        ),
        prompt_ref=prompt_ref,
    )
    return parse_score(answer)


def series_full_name(dataset: Dataset, codes: dict[str, str]) -> str:
    """Human-readable full name of a chosen dataset + code combination."""
    parts: list[str] = []
    for dimension in _ordered_dimensions(dataset):
        code = codes.get(dimension.code)
        if code is None:
            continue
        label = next(
            (row.label for row in dimension.codes if row.code == code),
            code,
        )
        parts.append(f"{dimension.label}: {label}")
    detail = "; ".join(parts)
    return f"{dataset.name} — {detail}" if detail else dataset.name


def indicator_request(dataset: Dataset, code: Any) -> str:
    """Plain-language request text for one Turcat indicator code."""
    attributes = code.attributes or {}
    bits = [code.label]
    if attributes.get("unit"):
        bits.append(f"birim: {attributes['unit']}")
    if attributes.get("frequency"):
        bits.append(f"frekans: {attributes['frequency']}")
    return f"{' | '.join(bits)} — {dataset.name}"


def indicator_codes(dataset: Dataset) -> list[Any]:
    """The non-group codes of the dataset's INDICATOR dimension."""
    dimension = next(
        (dim for dim in dataset.dimensions if dim.code == INDICATOR_DIMENSION),
        None,
    )
    if dimension is None:
        return []
    return [
        code
        for code in sorted(dimension.codes, key=lambda row: row.id or 0)
        if not (code.attributes or {}).get("group")
        and not (code.attributes or {}).get("removed_at")
    ]


def load_target_datasets(session: Session) -> list[Dataset]:
    """Load the databrowser2 datasets (institution ``tuik``) with their codes."""
    statement = (
        sa.select(Dataset)
        .join(Institution, Institution.id == Dataset.institution_id)
        .where(
            Institution.code == "tuik",
            Dataset.attributes["channel"].astext == "databrowser2",
        )
        .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
        .order_by(Dataset.source_category, Dataset.external_code)
    )
    return list(session.scalars(statement).all())


def upsert_link(
    session: Session,
    from_dataset_id: int,
    from_codes: dict[str, str],
    to_dataset_id: int,
    to_codes: dict[str, str],
    *,
    relation: str,
    method: str,
    confidence: float | None,
    note: str | None = None,
) -> CatalogLink:
    """Insert a link, or refresh a still-``proposed`` one (idempotent).

    Accepted/rejected rows are never overwritten: a review decision outranks a
    later automatic proposal.
    """
    existing = session.scalar(
        sa.select(CatalogLink).where(
            CatalogLink.from_dataset_id == from_dataset_id,
            CatalogLink.from_codes == from_codes,
            CatalogLink.to_dataset_id == to_dataset_id,
            CatalogLink.to_codes == to_codes,
        )
    )
    if existing is None:
        link = CatalogLink(
            from_dataset_id=from_dataset_id,
            from_codes=from_codes,
            to_dataset_id=to_dataset_id,
            to_codes=to_codes,
            mapping={},
            relation=relation,
            method=method,
            confidence=confidence,
            status=STATUS_PROPOSED,
            note=note,
        )
        session.add(link)
        session.flush()
        return link
    if existing.status == STATUS_PROPOSED:
        existing.relation = relation
        existing.method = method
        existing.confidence = confidence
        existing.note = note
        session.flush()
    return existing


def propose_links(
    session: Session,
    from_dataset: Dataset,
    jev_client: JevDecider,
    *,
    targets: Sequence[Dataset] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    prompt_ref: Any = None,
    same_series_threshold: float = SAME_SERIES_THRESHOLD,
    related_threshold: float = RELATED_THRESHOLD,
) -> list[LinkProposal]:
    """Propose a target series for each indicator of ``from_dataset``."""
    candidates = list(targets) if targets is not None else load_target_datasets(session)
    if not candidates:
        raise LinkingError("no databrowser2 datasets in the catalog")
    codes = indicator_codes(from_dataset)
    if limit is not None:
        codes = codes[:limit]

    proposals: list[LinkProposal] = []
    for code in codes:
        request = indicator_request(from_dataset, code)
        try:
            dataset, _dataset_conf = select_target_dataset(
                jev_client, request, candidates, prompt_ref=prompt_ref
            )
            to_codes, _code_conf = select_target_codes(
                jev_client, request, dataset, prompt_ref=prompt_ref
            )
            name = series_full_name(dataset, to_codes)
            score, _verify_conf = verify_match(jev_client, request, name, prompt_ref=prompt_ref)
        except LinkingError as exc:
            logger.warning("linking %s:%s failed: %s", from_dataset.external_code, code.code, exc)
            proposals.append(
                LinkProposal(
                    from_code=code.code,
                    from_name=code.label,
                    to_dataset_id=None,
                    to_dataset_code=None,
                    status="error",
                    note=str(exc),
                )
            )
            continue

        confidence = round(score, 4)
        if score < related_threshold:
            proposals.append(
                LinkProposal(
                    from_code=code.code,
                    from_name=code.label,
                    to_dataset_id=dataset.id,
                    to_dataset_code=dataset.external_code,
                    to_codes=to_codes,
                    to_name=name,
                    relation=RELATION_RELATED,
                    confidence=confidence,
                    status="skipped",
                    note=f"doğrulama düşük ({score:.2f})",
                )
            )
            continue
        relation = RELATION_SAME_SERIES if score >= same_series_threshold else RELATION_RELATED
        note = None if relation == RELATION_SAME_SERIES else f"doğrulama orta ({score:.2f})"
        proposal = LinkProposal(
            from_code=code.code,
            from_name=code.label,
            to_dataset_id=dataset.id,
            to_dataset_code=dataset.external_code,
            to_codes=to_codes,
            to_name=name,
            relation=relation,
            confidence=confidence,
            status=STATUS_PROPOSED,
            note=note,
        )
        if not dry_run and dataset.id is not None and from_dataset.id is not None:
            upsert_link(
                session,
                from_dataset.id,
                {INDICATOR_DIMENSION: code.code},
                dataset.id,
                to_codes,
                relation=relation,
                method=METHOD_JEV,
                confidence=confidence,
                note=note,
            )
            session.flush()
            proposal = replace(proposal, persisted=True)
        proposals.append(proposal)
    return proposals


# --- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.linking",
        description="Propose, list and review catalog links (Turcat -> databrowser2).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    propose = subparsers.add_parser("propose", help="propose links with Jev")
    propose.add_argument("--from-dataset", required=True, dest="from_dataset")
    propose.add_argument("--limit", type=int, default=None)
    propose.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="run the Jev flow but write no links",
    )

    listing = subparsers.add_parser("list", help="list links by status")
    listing.add_argument("--status", default=STATUS_PROPOSED)

    show = subparsers.add_parser("show", help="print one link including its mapping")
    show.add_argument("id", type=int)

    set_mapping = subparsers.add_parser(
        "set-mapping", help="validate and set a link's mapping JSON"
    )
    set_mapping.add_argument("id", type=int)
    set_mapping.add_argument("--json", required=True, dest="json")

    accept = subparsers.add_parser("accept", help="mark a link accepted")
    accept.add_argument("id", type=int)

    reject = subparsers.add_parser("reject", help="mark a link rejected")
    reject.add_argument("id", type=int)
    reject.add_argument("--note", default=None)

    return parser


def _load_from_dataset(session: Session, external_code: str) -> Dataset:
    dataset = session.scalar(
        sa.select(Dataset)
        .join(Institution, Institution.id == Dataset.institution_id)
        .where(Dataset.external_code == external_code)
        .options(selectinload(Dataset.dimensions).selectinload(DatasetDimension.codes))
    )
    if dataset is None:
        raise LinkingError(f"no catalog dataset {external_code!r}")
    return dataset


def _cmd_propose(session: Session, args: argparse.Namespace) -> int:
    from app.config import settings
    from app.llm.jev import JevClient

    from_dataset = _load_from_dataset(session, args.from_dataset)
    jev = JevClient(settings)
    try:
        proposals = propose_links(
            session,
            from_dataset,
            jev,
            limit=args.limit,
            dry_run=bool(args.dry_run),
        )
    finally:
        jev.close()
    if not args.dry_run:
        session.commit()
    mode = " (dry-run)" if args.dry_run else ""
    print(f"propose{mode}: {len(proposals)} indicators")
    for proposal in proposals:
        print(
            f"  {proposal.from_code}\t{proposal.status}\t{proposal.relation}\t"
            f"{proposal.confidence}\t{proposal.to_dataset_code}\t"
            f"{proposal.to_codes}\t{proposal.note or ''}"
        )
    return 0


def _cmd_list(session: Session, args: argparse.Namespace) -> int:
    rows = session.scalars(
        sa.select(CatalogLink)
        .where(CatalogLink.status == args.status)
        .order_by(CatalogLink.id)
        .limit(200)
    ).all()
    for row in rows:
        print(
            f"{row.id}\t{row.status}\t{row.relation}\t{row.confidence}\t"
            f"from={row.from_dataset_id}{row.from_codes} "
            f"to={row.to_dataset_id}{row.to_codes}\t"
            f"mapping={mapping_summary(row.mapping)}\t{row.note or ''}"
        )
    print(f"list: {len(rows)} rows (status={args.status}, limit 200)")
    return 0


def _cmd_show(session: Session, args: argparse.Namespace) -> int:
    link = session.get(CatalogLink, args.id)
    if link is None:
        print(f"show: no link with id {args.id}", file=sys.stderr)
        return 1
    mapping = to_storage(parse_mapping(link.mapping))
    print(
        f"{link.id}\t{link.status}\t{link.relation}\tmethod={link.method}\t"
        f"confidence={link.confidence}"
    )
    print(f"from={link.from_dataset_id}{link.from_codes}")
    print(f"to={link.to_dataset_id}{link.to_codes}")
    print(f"mapping={json.dumps(mapping, ensure_ascii=False)}")
    if link.note:
        print(f"note={link.note}")
    return 0


def _cmd_set_mapping(session: Session, args: argparse.Namespace) -> int:
    link = session.get(CatalogLink, args.id)
    if link is None:
        print(f"set-mapping: no link with id {args.id}", file=sys.stderr)
        return 1
    mapping = set_link_mapping(link, mapping_from_json(args.json))
    session.commit()
    stored = json.dumps(to_storage(mapping), ensure_ascii=False)
    print(f"set-mapping: link {args.id} mapping={stored}")
    return 0


def _review(session: Session, link_id: int, status: str, note: str | None = None) -> int:
    link = session.get(CatalogLink, link_id)
    if link is None:
        print(f"review: no link with id {link_id}", file=sys.stderr)
        return 1
    link.status = status
    link.reviewed_at = datetime.now(UTC)
    if note is not None:
        link.note = note
    session.commit()
    print(f"review: link {link_id} -> {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            if args.command == "propose":
                return _cmd_propose(session, args)
            if args.command == "list":
                return _cmd_list(session, args)
            if args.command == "show":
                return _cmd_show(session, args)
            if args.command == "set-mapping":
                return _cmd_set_mapping(session, args)
            if args.command == "accept":
                return _review(session, args.id, STATUS_ACCEPTED)
            return _review(session, args.id, STATUS_REJECTED, args.note)
    except (LinkingError, MappingError) as exc:
        print(f"{args.command}: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_CHOICE_OPTIONS",
    "METHOD_JEV",
    "METHOD_MANUAL",
    "RELATION_RELATED",
    "RELATION_SAME_SERIES",
    "STATUS_ACCEPTED",
    "STATUS_PROPOSED",
    "STATUS_REJECTED",
    "ChoiceAnswer",
    "JevDecider",
    "LinkProposal",
    "LinkingError",
    "build_parser",
    "choice_question",
    "indicator_codes",
    "indicator_request",
    "main",
    "narrow_choice",
    "noul_question",
    "parse_choice",
    "parse_score",
    "propose_links",
    "select_dimension_code",
    "select_target_codes",
    "select_target_dataset",
    "series_full_name",
    "upsert_link",
    "verify_match",
]
