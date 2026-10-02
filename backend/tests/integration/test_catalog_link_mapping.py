"""Integration tests for catalog link mappings (isolated ``karven-test`` stack)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.catalog import regions
from app.catalog.mapping import MappingError, normalize_mapping, resolve_target
from app.data.models import CatalogLink, Dataset, Institution, RegionCrosswalk
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration

RELATION = "same_series"
METHOD = "manual"
STATUS = "accepted"


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _make_institution(session) -> Institution:
    institution = Institution(code=_unique("inst"), name="link mapping test")
    session.add(institution)
    session.flush()
    return institution


def _make_dataset(session, institution_id: int, external_code: str) -> Dataset:
    dataset = Dataset(
        institution_id=institution_id,
        external_code=external_code,
        name=external_code,
    )
    session.add(dataset)
    session.flush()
    return dataset


def _mapping() -> dict:
    return {
        "region": {
            "from_dim": "REF_AREA",
            "to_dim": "IKAMET_YERI",
            "from_scheme": regions.SCHEME_CIP_PLATE,
            "to_scheme": regions.SCHEME_IBBS,
            "level": regions.LEVEL_PROVINCE,
        },
        "period": {"rule": "month_dimension", "dim": "AY", "format": "M%02d"},
        "scale": "1000",
    }


def test_catalog_link_mapping_persists_and_round_trips() -> None:
    mapping = _mapping()
    with SessionLocal() as session:
        institution = _make_institution(session)
        source = _make_dataset(session, institution.id, _unique("CIP_SRC"))
        target = _make_dataset(session, institution.id, _unique("DF_DST"))
        link = CatalogLink(
            from_dataset_id=source.id,
            from_codes={"REF_AREA": "6"},
            to_dataset_id=target.id,
            to_codes={"REF_AREA": "TR"},
            mapping=mapping,
            relation=RELATION,
            method=METHOD,
            status=STATUS,
        )
        session.add(link)
        session.commit()
        link_id = link.id

    with SessionLocal() as session:
        loaded = session.get(CatalogLink, link_id)
        assert loaded is not None
        assert loaded.mapping == mapping
        assert normalize_mapping(loaded.mapping) == mapping


def test_catalog_link_mapping_defaults_to_empty_object() -> None:
    with SessionLocal() as session:
        institution = _make_institution(session)
        source = _make_dataset(session, institution.id, _unique("CIP_SRC"))
        target = _make_dataset(session, institution.id, _unique("DF_DST"))
        link = CatalogLink(
            from_dataset_id=source.id,
            from_codes={"REF_AREA": "6"},
            to_dataset_id=target.id,
            to_codes={},
            relation=RELATION,
            method=METHOD,
            status=STATUS,
        )
        session.add(link)
        session.commit()
        link_id = link.id

    with SessionLocal() as session:
        loaded = session.get(CatalogLink, link_id)
        assert loaded is not None
        assert loaded.mapping == {}


def test_resolve_target_against_seeded_crosswalk() -> None:
    scheme = _unique("scheme")
    with SessionLocal() as session:
        institution = _make_institution(session)
        source = _make_dataset(session, institution.id, _unique("CIP_SRC"))
        target = _make_dataset(session, institution.id, _unique("DF_DST"))
        session.add(
            RegionCrosswalk(
                from_scheme=scheme,
                from_code="6",
                to_scheme=regions.SCHEME_IBBS,
                to_code="TR510",
                level=regions.LEVEL_PROVINCE,
                label="Ankara",
                method=regions.METHOD_LABEL_MATCH,
            )
        )
        link = CatalogLink(
            from_dataset_id=source.id,
            from_codes={"REF_AREA": "6"},
            to_dataset_id=target.id,
            to_codes={"FREQ": "M"},
            mapping={
                "region": {
                    "from_dim": "REF_AREA",
                    "to_dim": "REF_AREA",
                    "from_scheme": scheme,
                    "to_scheme": regions.SCHEME_IBBS,
                    "level": regions.LEVEL_PROVINCE,
                },
                "period": {"rule": "month_of_year", "month": 12},
                "scale": "1000",
            },
            relation=RELATION,
            method=METHOD,
            status=STATUS,
        )
        session.add(link)
        session.commit()
        link_id = link.id
        target_code = target.external_code

    with SessionLocal() as session:
        link = session.get(CatalogLink, link_id)
        assert link is not None
        key = resolve_target(session, link, {"REF_AREA": "6"}, date(2025, 1, 1))
        assert key.dataset_code == target_code
        assert key.codes == {"FREQ": "M", "REF_AREA": "TR510"}
        assert key.period == date(2025, 12, 1)
        assert key.scale == Decimal("1000")

    with SessionLocal() as session:
        link = session.get(CatalogLink, link_id)
        assert link is not None
        with pytest.raises(MappingError):
            resolve_target(session, link, {"REF_AREA": "999"}, date(2025, 1, 1))
