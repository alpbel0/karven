"""Region code crosswalks (source-independent), reusable by Phase 3 maps.

The first crosswalk maps TÜİK CİP province plate codes (``1``..``81``) to İBBS
level-3 province codes (``TR100``, ``TR211``, ``TRA11``, ...). It is stored in
``region_crosswalk`` — a table with no dataset foreign key — so other sources
(e.g. TCMB province data) can add their own schemes later.

Matching: a CİP province label (upper-case Turkish, e.g. ``KIRKLARELİ``) is
compared with the İBBS level-3 label (title case, e.g. ``Kırklareli``) after
:func:`turkish_fold`. Two CİP labels are spelling errors and are mapped by an
explicit reviewed override (``method='manual'``). Every row must also satisfy
the structural check that the İBBS code starts with the CİP ``parent_code``
(the İBBS level-2 region).

CLI::

    python -m app.catalog.regions build-cip-provinces [--dry-run]
    python -m app.catalog.regions list [--from-scheme cip_plate]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data.models import Dataset, DatasetDimension, DimensionCode, RegionCrosswalk

logger = logging.getLogger("app.catalog.regions")

SCHEME_CIP_PLATE = "cip_plate"
SCHEME_IBBS = "ibbs"
LEVEL_PROVINCE = "province"
METHOD_LABEL_MATCH = "label_match"
METHOD_MANUAL = "manual"

REF_AREA = "REF_AREA"
PROVINCE_COUNT = 81
# İBBS level-3 codes: TR + one region char (1-9, A-C) + two digits.
IBBS3_PATTERN = re.compile(r"^TR[0-9A-C][0-9]{2}$")
# ``CIP_`` prefix, with the underscore escaped so it is literal.
CIP_CODE_LIKE = "CIP!_%"
CIP_CODE_ESCAPE = "!"


class CrosswalkError(Exception):
    """A crosswalk build failure (unmatched province, count mismatch, ...)."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n  - ".join(self.problems))


@dataclass(frozen=True)
class ProvinceCode:
    """One CİP province: its plate code, upper-case label and İBBS-2 parent."""

    code: str
    label: str
    parent_code: str | None


@dataclass(frozen=True)
class IbbsProvince:
    """One İBBS level-3 province: its code and canonical (title case) label."""

    code: str
    label: str


@dataclass(frozen=True)
class ManualOverride:
    """A reviewed fix for a CİP label that would not match by folding."""

    to_code: str
    note: str


@dataclass(frozen=True)
class CrosswalkRow:
    """One row of the crosswalk, mirroring the ``region_crosswalk`` columns."""

    from_scheme: str
    from_code: str
    to_scheme: str
    to_code: str
    level: str
    label: str
    method: str
    note: str | None = None


@dataclass(frozen=True)
class CrosswalkPlan:
    """Matching result: the rows produced and any per-row problems."""

    rows: tuple[CrosswalkRow, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when no per-row problem was found (count check is separate)."""
        return not self.problems


@dataclass(frozen=True)
class BuildSummary:
    """Result of a ``build-cip-provinces`` run, for reporting."""

    total: int
    matched: int
    manual: int


# The only two CİP labels that do not fold-match their İBBS label. Verified by
# the reviewer: KAYSERI (dotless I) and NEVSEHİR (H instead of Ş).
MANUAL_OVERRIDES: dict[str, ManualOverride] = {
    "38": ManualOverride(
        to_code="TR721",
        note=(
            "CİP labels Kayseri 'KAYSERI' (dotless I); Turkish folding gives "
            "'kayserı', which does not match İBBS 'Kayseri'. Reviewed override."
        ),
    ),
    "50": ManualOverride(
        to_code="TR714",
        note=(
            "CİP labels Nevşehir 'NEVSEHİR' (H instead of Ş); folding gives "
            "'nevsehir', which does not match İBBS 'Nevşehir'. Reviewed override."
        ),
    ),
}


def turkish_fold(text: str) -> str:
    """Fold Turkish text for label comparison.

    Python's ``str.lower`` maps ``I`` to ``i`` and ``İ`` to ``i`` + a combining
    dot, both wrong for Turkish. Replacing the dotted/dotless capitals first and
    only then lower-casing yields the fold that compares CİP upper-case labels
    (``İSTANBUL``) with İBBS title-case labels (``İstanbul``).
    """
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_province_plate(code: str) -> bool:
    """True for a CİP province plate code (``1``..``81``)."""
    return code.isdigit() and 1 <= int(code) <= PROVINCE_COUNT


def _plate_sort_key(province: ProvinceCode) -> tuple[int, str]:
    return (int(province.code), province.code) if province.code.isdigit() else (0, province.code)


def match_crosswalk(
    cip_provinces: Sequence[ProvinceCode],
    ibbs_provinces: Sequence[IbbsProvince],
    *,
    overrides: Mapping[str, ManualOverride] = MANUAL_OVERRIDES,
) -> CrosswalkPlan:
    """Match CİP provinces to İBBS provinces by folded label + parent check.

    Pure: no database access. Every produced row's İBBS code must start with the
    CİP ``parent_code``; a mismatch is recorded as a problem, not a row error,
    so the caller refuses to write. Overrides force a reviewed İBBS code and
    mark the row ``manual``.
    """
    ibbs_by_code = {province.code: province for province in ibbs_provinces}
    by_fold: dict[str, list[str]] = {}
    for province in ibbs_provinces:
        by_fold.setdefault(turkish_fold(province.label), []).append(province.code)

    rows: list[CrosswalkRow] = []
    problems: list[str] = []
    for province in sorted(cip_provinces, key=_plate_sort_key):
        override = overrides.get(province.code)
        if override is not None:
            target = ibbs_by_code.get(override.to_code)
            if target is None:
                problems.append(
                    f"CİP {province.code} ({province.label}): override İBBS code "
                    f"{override.to_code} was not found in the catalog"
                )
                continue
            to_code = override.to_code
            method = METHOD_MANUAL
            note = override.note
        else:
            candidates = by_fold.get(turkish_fold(province.label), [])
            if len(candidates) != 1:
                problems.append(
                    f"CİP {province.code} ({province.label}): expected one İBBS "
                    f"label match, found {len(candidates)} {candidates}"
                )
                continue
            to_code = candidates[0]
            target = ibbs_by_code[to_code]
            method = METHOD_LABEL_MATCH
            note = None

        if province.parent_code and not to_code.startswith(province.parent_code):
            problems.append(
                f"CİP {province.code} ({province.label}) parent {province.parent_code} "
                f"is not a prefix of İBBS {to_code}"
            )
            continue

        rows.append(
            CrosswalkRow(
                from_scheme=SCHEME_CIP_PLATE,
                from_code=province.code,
                to_scheme=SCHEME_IBBS,
                to_code=to_code,
                level=LEVEL_PROVINCE,
                label=target.label,
                method=method,
                note=note,
            )
        )
    return CrosswalkPlan(rows=tuple(rows), problems=tuple(problems))


def validate_crosswalk(
    rows: Sequence[CrosswalkRow], *, expected: int = PROVINCE_COUNT
) -> list[str]:
    """Return the count/1:1 problems that must block a write (empty when fine)."""
    problems: list[str] = []
    from_codes = [row.from_code for row in rows]
    to_codes = [row.to_code for row in rows]
    if len(from_codes) != expected:
        problems.append(f"expected {expected} provinces, got {len(from_codes)}")
    if len(set(from_codes)) != len(from_codes):
        problems.append("duplicate CİP province codes")
    if len(set(to_codes)) != len(to_codes):
        problems.append("İBBS codes are not 1:1 (a code is mapped more than once)")
    if len(set(to_codes)) != expected:
        problems.append(f"expected {expected} distinct İBBS codes, got {len(set(to_codes))}")
    return problems


# --- database access --------------------------------------------------------


def load_cip_provinces(session: Session) -> list[ProvinceCode]:
    """Read the CİP province codes from every ``CIP_%`` dataset's REF_AREA."""
    statement = (
        sa.select(
            Dataset.external_code,
            DimensionCode.code,
            DimensionCode.label,
            DimensionCode.parent_code,
        )
        .join(DatasetDimension, DatasetDimension.dataset_id == Dataset.id)
        .join(DimensionCode, DimensionCode.dimension_id == DatasetDimension.id)
        .where(
            Dataset.external_code.like(CIP_CODE_LIKE, escape=CIP_CODE_ESCAPE),
            DatasetDimension.code == REF_AREA,
        )
    )
    provinces: dict[str, ProvinceCode] = {}
    for _external_code, code, label, parent in session.execute(statement):
        if not is_province_plate(code):
            continue
        existing = provinces.get(code)
        if existing is not None and (existing.label != label or existing.parent_code != parent):
            raise CrosswalkError(
                [
                    f"CİP province {code} has conflicting definitions: "
                    f"({existing.label}, {existing.parent_code}) vs ({label}, {parent})"
                ]
            )
        provinces[code] = ProvinceCode(code=code, label=label, parent_code=parent)
    return list(provinces.values())


def load_ibbs_provinces(session: Session) -> list[IbbsProvince]:
    """Read the İBBS level-3 codes from every databrowser2 dataset's REF_AREA."""
    statement = (
        sa.select(DimensionCode.code, DimensionCode.label)
        .select_from(Dataset)
        .join(DatasetDimension, DatasetDimension.dataset_id == Dataset.id)
        .join(DimensionCode, DimensionCode.dimension_id == DatasetDimension.id)
        .where(
            Dataset.attributes["channel"].astext == "databrowser2",
            DatasetDimension.code == REF_AREA,
        )
    )
    provinces: dict[str, IbbsProvince] = {}
    for code, label in session.execute(statement):
        if not IBBS3_PATTERN.match(code):
            continue
        existing = provinces.get(code)
        if existing is not None and existing.label != label:
            raise CrosswalkError(
                [f"İBBS province {code} has conflicting labels: {existing.label!r} vs {label!r}"]
            )
        provinces[code] = IbbsProvince(code=code, label=label)
    return list(provinces.values())


def upsert_crosswalk(session: Session, row: CrosswalkRow) -> RegionCrosswalk:
    """Insert a row, or refresh the existing one (idempotent re-run)."""
    existing = session.scalar(
        sa.select(RegionCrosswalk).where(
            RegionCrosswalk.from_scheme == row.from_scheme,
            RegionCrosswalk.from_code == row.from_code,
            RegionCrosswalk.to_scheme == row.to_scheme,
        )
    )
    if existing is None:
        model = RegionCrosswalk(**asdict(row))
        session.add(model)
        session.flush()
        return model
    existing.to_code = row.to_code
    existing.level = row.level
    existing.label = row.label
    existing.method = row.method
    existing.note = row.note
    session.flush()
    return existing


def build_cip_provinces(
    session: Session,
    *,
    dry_run: bool = False,
    overrides: Mapping[str, ManualOverride] = MANUAL_OVERRIDES,
) -> BuildSummary:
    """Build the CİP plate -> İBBS province crosswalk, refusing bad data.

    Reads both code schemes from the catalog, matches and validates, and writes
    nothing unless exactly 81 distinct provinces map 1:1 to 81 distinct İBBS
    codes. With ``dry_run`` it only validates.
    """
    plan = match_crosswalk(
        load_cip_provinces(session),
        load_ibbs_provinces(session),
        overrides=overrides,
    )
    problems = [*plan.problems, *validate_crosswalk(plan.rows)]
    if problems:
        raise CrosswalkError(problems)

    if not dry_run:
        for row in plan.rows:
            upsert_crosswalk(session, row)
        session.flush()

    matched = sum(1 for row in plan.rows if row.method == METHOD_LABEL_MATCH)
    manual = sum(1 for row in plan.rows if row.method == METHOD_MANUAL)
    return BuildSummary(total=len(plan.rows), matched=matched, manual=manual)


def list_crosswalk(session: Session, from_scheme: str) -> list[RegionCrosswalk]:
    """Every crosswalk row for one source scheme, oldest first."""
    statement = (
        sa.select(RegionCrosswalk)
        .where(RegionCrosswalk.from_scheme == from_scheme)
        .order_by(RegionCrosswalk.id)
    )
    return list(session.scalars(statement).all())


def to_ibbs(session: Session, scheme: str, code: str) -> str | None:
    """Translate a code in ``scheme`` to its İBBS province code, if known."""
    return session.scalar(
        sa.select(RegionCrosswalk.to_code).where(
            RegionCrosswalk.from_scheme == scheme,
            RegionCrosswalk.from_code == code,
            RegionCrosswalk.to_scheme == SCHEME_IBBS,
            RegionCrosswalk.level == LEVEL_PROVINCE,
        )
    )


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.catalog.regions",
        description="Build and list region code crosswalks (CİP <-> İBBS).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build-cip-provinces",
        help="build the CİP plate -> İBBS province crosswalk (81 provinces)",
    )
    build.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="validate and report but write no rows",
    )

    listing = subparsers.add_parser("list", help="list crosswalk rows")
    listing.add_argument("--from-scheme", default=SCHEME_CIP_PLATE, dest="from_scheme")

    return parser


def _cmd_build(session: Session, args: argparse.Namespace) -> int:
    summary = build_cip_provinces(session, dry_run=bool(args.dry_run))
    if not args.dry_run:
        session.commit()
    mode = " (dry-run)" if args.dry_run else ""
    print(
        f"build-cip-provinces{mode}: {summary.total} rows, "
        f"{summary.matched} label_match, {summary.manual} manual, 0 problems"
    )
    return 0


def _cmd_list(session: Session, args: argparse.Namespace) -> int:
    rows = list_crosswalk(session, args.from_scheme)
    for row in rows:
        print(
            f"{row.id}\t{row.from_scheme}:{row.from_code} -> "
            f"{row.to_scheme}:{row.to_code}\t{row.level}\t{row.label}\t"
            f"{row.method}\t{row.note or ''}"
        )
    print(f"list: {len(rows)} rows (from_scheme={args.from_scheme})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args: Any = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.db.session import SessionLocal

    try:
        with SessionLocal() as session:
            if args.command == "build-cip-provinces":
                return _cmd_build(session, args)
            return _cmd_list(session, args)
    except CrosswalkError as exc:
        print(f"{args.command}: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CIP_CODE_ESCAPE",
    "CIP_CODE_LIKE",
    "IBBS3_PATTERN",
    "LEVEL_PROVINCE",
    "MANUAL_OVERRIDES",
    "METHOD_LABEL_MATCH",
    "METHOD_MANUAL",
    "PROVINCE_COUNT",
    "SCHEME_CIP_PLATE",
    "SCHEME_IBBS",
    "BuildSummary",
    "CrosswalkError",
    "CrosswalkPlan",
    "CrosswalkRow",
    "IbbsProvince",
    "ManualOverride",
    "ProvinceCode",
    "build_cip_provinces",
    "build_parser",
    "is_province_plate",
    "list_crosswalk",
    "load_cip_provinces",
    "load_ibbs_provinces",
    "main",
    "match_crosswalk",
    "to_ibbs",
    "turkish_fold",
    "upsert_crosswalk",
    "validate_crosswalk",
]
