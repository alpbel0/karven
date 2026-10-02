"""Integration tests for the region crosswalk (isolated ``karven-test`` stack)."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.catalog import regions
from app.data.models import (
    Dataset,
    DatasetDimension,
    DimensionCode,
    Institution,
    RegionCrosswalk,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration

# 12 İBBS level-1 region characters.
_REGION_CHARS = "123456789ABC"
_SYNTHETIC_COUNT = 81
CIP_SCHEME = regions.SCHEME_CIP_PLATE


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _synthetic_ibbs_codes() -> list[str]:
    """81 distinct İBBS-3-shaped codes (all in region 1 in this fixture)."""
    codes = [f"TR{region}{n:02d}" for region in _REGION_CHARS for n in range(100)]
    return codes[:_SYNTHETIC_COUNT]


def _seed_catalog(session) -> list[str]:
    """Seed a mini catalog: 81 CİP plate provinces + 81 databrowser2 İBBS ones."""
    codes = _synthetic_ibbs_codes()
    institution = Institution(code=_unique("inst"), name="region crosswalk test")
    session.add(institution)
    session.flush()

    cip = Dataset(
        institution_id=institution.id,
        external_code=f"CIP_{_unique('TEST')}",
        name="cip test",
    )
    cip.attributes = {"channel": "cip"}
    cip.dimensions = [
        DatasetDimension(
            code="REF_AREA",
            label="Yer",
            position=0,
            role="geo",
            codes=[
                DimensionCode(
                    code=str(index + 1),
                    label=f"PROV{index + 1}",
                    parent_code=codes[index][:4],
                )
                for index in range(_SYNTHETIC_COUNT)
            ],
        )
    ]

    db2 = Dataset(
        institution_id=institution.id,
        external_code=_unique("DF_TEST"),
        name="databrowser2 test",
    )
    db2.attributes = {"channel": "databrowser2"}
    db2.dimensions = [
        DatasetDimension(
            code="REF_AREA",
            label="Bölge",
            position=0,
            role="geo",
            codes=[
                DimensionCode(code=code, label=f"Prov{index + 1}")
                for index, code in enumerate(codes)
            ],
        )
    ]

    session.add_all([cip, db2])
    session.commit()
    return codes


def _cip_row_count(session) -> int:
    return session.scalar(
        sa.select(sa.func.count())
        .select_from(RegionCrosswalk)
        .where(RegionCrosswalk.from_scheme == CIP_SCHEME)
    )


def test_region_crosswalk_table_and_constraints() -> None:
    scheme = _unique("scheme")
    with SessionLocal() as session:
        session.add(
            RegionCrosswalk(
                from_scheme=scheme,
                from_code="1",
                to_scheme=regions.SCHEME_IBBS,
                to_code="TR100",
                level=regions.LEVEL_PROVINCE,
                label="İstanbul",
                method=regions.METHOD_LABEL_MATCH,
            )
        )
        session.commit()

    with SessionLocal() as session:
        session.add(
            RegionCrosswalk(
                from_scheme=scheme,
                from_code="1",
                to_scheme=regions.SCHEME_IBBS,
                to_code="TR999",
                level=regions.LEVEL_PROVINCE,
                label="dup",
                method=regions.METHOD_MANUAL,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with SessionLocal() as session:
        session.add(
            RegionCrosswalk(
                from_scheme=scheme,
                from_code="2",
                to_scheme=regions.SCHEME_IBBS,
                to_code="TR200",
                level=regions.LEVEL_PROVINCE,
                label="bad method",
                method="automatic",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with SessionLocal() as session:
        session.add(
            RegionCrosswalk(
                from_scheme=scheme,
                from_code="3",
                to_scheme=regions.SCHEME_IBBS,
                to_code="TR300",
                level="country",
                label="bad level",
                method=regions.METHOD_MANUAL,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_build_cip_provinces_is_idempotent_on_a_seeded_catalog() -> None:
    with SessionLocal() as session:
        codes = _seed_catalog(session)

    with SessionLocal() as session:
        summary = regions.build_cip_provinces(session, overrides={})
        session.commit()

    assert summary.total == _SYNTHETIC_COUNT
    assert summary.matched == _SYNTHETIC_COUNT
    assert summary.manual == 0

    with SessionLocal() as session:
        assert _cip_row_count(session) == _SYNTHETIC_COUNT
        assert regions.to_ibbs(session, CIP_SCHEME, "1") == codes[0]
        assert regions.to_ibbs(session, CIP_SCHEME, "81") == codes[80]

        # Re-run: the unique (from_scheme, from_code, to_scheme) key refreshes
        # the same 81 rows instead of inserting duplicates.
        again = regions.build_cip_provinces(session, overrides={})
        session.commit()

    assert again == summary
    with SessionLocal() as session:
        assert _cip_row_count(session) == _SYNTHETIC_COUNT


def test_build_cip_provinces_dry_run_writes_nothing() -> None:
    with SessionLocal() as session:
        _seed_catalog(session)

    with SessionLocal() as session:
        before = _cip_row_count(session)
        summary = regions.build_cip_provinces(session, dry_run=True, overrides={})
        session.rollback()
        after = _cip_row_count(session)

    assert summary.total == _SYNTHETIC_COUNT
    assert before == after
