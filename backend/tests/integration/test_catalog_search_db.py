"""Integration test: catalog series search against a real session (Task 2.4).

Written for the isolated ``karven-test`` stack; intentionally NOT run by the unit
suite. Run with ``uv run python scripts/integration.py``.

The test DB is shared with every other test file, so each test:
- uses get-or-create institutions (never inserts a duplicate ``tuik``),
- owns unique dataset external codes (a uuid suffix) and deletes its rows in a
  fixture teardown,
- seeds the default prompts with ``ensure_default_prompts``.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.catalog import search
from app.catalog.search import ensure_default_prompts, load_search_prompts, search_series
from app.catalog.tree import load_tree
from app.connectors.base import build_series_definition
from app.data.models import (
    CatalogLink,
    Dataset,
    DatasetDimension,
    DatasetTag,
    DimensionCode,
    Institution,
    MeasureCombination,
    Series,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


class _FakeSearchJev:
    """Every score answer passes; every choice answer takes the first option."""

    def decide(self, state, questions, *, prompt_ref=None):  # noqa: ANN001
        answers: dict[str, dict] = {}
        for question_id, question in questions.items():
            if question["type"] == "score":
                answers[question_id] = {
                    "type": "score",
                    "score": 4,
                    "probabilities": [0.0, 0.0, 0.0, 0.1, 0.9],
                }
            elif question["type"] == "choice":
                answers[question_id] = {
                    "choice": next(iter(question["criteria"])),
                    "confidence": 0.95,
                }
            else:
                answers[question_id] = {"noul": 0.9}
        return {"answers": answers, "usage": {}, "provider": "fake", "model": "fake"}


class _FakeSearchChat:
    def complete(self, messages, *, response_format=None, prompt_ref=None, **kwargs):  # noqa: ANN001
        return SimpleNamespace(content='{"phrasings": ["bir", "iki"]}')


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


@pytest.fixture
def seeded_codes() -> list[str]:
    """Collect the external codes a test creates and delete their rows after."""
    codes: list[str] = []
    yield codes
    if not codes:
        return
    with SessionLocal() as session:
        ids = list(
            session.scalars(sa.select(Dataset.id).where(Dataset.external_code.in_(codes))).all()
        )
        if not ids:
            return
        session.execute(sa.delete(DatasetTag).where(DatasetTag.dataset_id.in_(ids)))
        session.execute(sa.delete(MeasureCombination).where(MeasureCombination.dataset_id.in_(ids)))
        dimension_ids = list(
            session.scalars(
                sa.select(DatasetDimension.id).where(DatasetDimension.dataset_id.in_(ids))
            ).all()
        )
        if dimension_ids:
            session.execute(
                sa.delete(DimensionCode).where(DimensionCode.dimension_id.in_(dimension_ids))
            )
        session.execute(sa.delete(DatasetDimension).where(DatasetDimension.dataset_id.in_(ids)))
        session.execute(
            sa.delete(CatalogLink).where(
                sa.or_(
                    CatalogLink.from_dataset_id.in_(ids),
                    CatalogLink.to_dataset_id.in_(ids),
                )
            )
        )
        session.execute(sa.delete(Dataset).where(Dataset.id.in_(ids)))
        session.commit()


def _institution(session, code: str) -> Institution:
    institution = session.scalar(sa.select(Institution).where(Institution.code == code))
    if institution is None:
        institution = Institution(code=code, name=code)
        session.add(institution)
        session.flush()
    return institution


def _seed_dataset(
    session,
    code: str,
    *,
    name: str = "Arama Testi",
    arsiv: bool = False,
    revizyon: bool = False,
    cok_konulu: bool = False,
) -> int:
    dataset = Dataset(
        institution_id=_institution(session, "tuik").id,
        external_code=code,
        name=name,
        arsiv=arsiv,
        revizyon_tablosu=revizyon,
        cok_konulu_derleme=cok_konulu,
        attributes={},
    )
    session.add(dataset)
    session.flush()
    dimension = DatasetDimension(
        dataset_id=dataset.id, code="INDICATOR", label="Gösterge", position=0, role="other"
    )
    session.add(dimension)
    session.flush()
    session.add(
        DimensionCode(
            dimension_id=dimension.id,
            code="POP",
            label="Nüfus",
            attributes={"unit": "Kişi", "frequency": "monthly"},
        )
    )
    session.commit()
    return dataset.id


def _seed_tag(session, dataset_id: int, tag_id: str, level: str, status: str) -> None:
    session.add(
        DatasetTag(
            dataset_id=dataset_id,
            tag_id=tag_id,
            level=level,
            confidence=0.9,
            source="jev",
            status=status,
        )
    )
    session.commit()


def test_candidate_sql_leaf_branch_revizyon_arsiv_filters(seeded_codes: list[str]) -> None:
    tree = load_tree()
    leaf = tree.leaf_order[0]
    other_leaf = tree.leaf_order[1]
    branch = tree.branch_order[0]
    other_branch = tree.branch_order[1]

    keep = _unique("SQLKEEP")
    review = _unique("SQLREV")
    wrong_leaf = _unique("SQLWL")
    cok = _unique("SQLCOK")
    wrong_branch = _unique("SQLWB")
    revision = _unique("SQLRZ")
    archive = _unique("SQLAR")
    seeded_codes.extend([keep, review, wrong_leaf, cok, wrong_branch, revision, archive])

    with SessionLocal() as session:
        keep_id = _seed_dataset(session, keep)
        review_id = _seed_dataset(session, review)
        wrong_leaf_id = _seed_dataset(session, wrong_leaf)
        cok_id = _seed_dataset(session, cok, cok_konulu=True)
        wrong_branch_id = _seed_dataset(session, wrong_branch, cok_konulu=True)
        revision_id = _seed_dataset(session, revision, revizyon=True)
        archive_id = _seed_dataset(session, archive, arsiv=True)

        _seed_tag(session, keep_id, leaf, "leaf", "accepted")
        _seed_tag(session, review_id, leaf, "leaf", "review")
        _seed_tag(session, wrong_leaf_id, other_leaf, "leaf", "accepted")
        _seed_tag(session, cok_id, branch, "branch", "accepted")
        _seed_tag(session, wrong_branch_id, other_branch, "branch", "accepted")
        _seed_tag(session, revision_id, leaf, "leaf", "accepted")
        _seed_tag(session, archive_id, leaf, "leaf", "accepted")

    with SessionLocal() as session:
        live = search._load_candidate_datasets(
            session, leaf_ids=[leaf], branch_ids=[branch], arsiv="exclude"
        )
        live_codes = {row[0].external_code for row in live}
        assert keep in live_codes
        assert cok in live_codes
        assert review not in live_codes
        assert wrong_leaf not in live_codes
        assert wrong_branch not in live_codes
        assert revision not in live_codes
        assert archive not in live_codes

        archived = search._load_candidate_datasets(
            session, leaf_ids=[leaf], branch_ids=[branch], arsiv="only"
        )
        archived_codes = {row[0].external_code for row in archived}
        assert archive in archived_codes
        assert keep not in archived_codes


def test_search_series_is_read_only(seeded_codes: list[str]) -> None:
    tree = load_tree()
    leaf = tree.leaf_order[0]
    code = _unique("READONLY")
    seeded_codes.append(code)

    with SessionLocal() as session:
        dataset_id = _seed_dataset(session, code, name="Konut Satışları")
        _seed_tag(session, dataset_id, leaf, "leaf", "accepted")

    with SessionLocal() as session:
        ensure_default_prompts(session)
        session.commit()
        bodies, refs = load_search_prompts(session)
        series_before = session.scalar(sa.select(sa.func.count()).select_from(Series)) or 0

        result = search_series(
            session,
            "Türkiye geneli toplam nüfus, aylık",
            jev=_FakeSearchJev(),
            chat=_FakeSearchChat(),
            tree=tree,
            bodies=bodies,
            refs=refs,
            include_trace=True,
        )

        assert result["status"] == "ok"
        assert result["strong"]
        item = result["strong"][0]
        assert item["dataset"] == code
        assert item["codes"] == {"INDICATOR": "POP"}
        assert "value" not in item and "observations" not in item

        # No pending/dirty/new objects and no new series row.
        assert list(session.new) == []
        assert list(session.dirty) == []
        assert list(session.deleted) == []
        series_after = session.scalar(sa.select(sa.func.count()).select_from(Series)) or 0
        assert series_after == series_before


def test_build_series_definition_on_hierarchical_dimension(seeded_codes: list[str]) -> None:
    code = _unique("HIER")
    seeded_codes.append(code)
    with SessionLocal() as session:
        dataset = Dataset(
            institution_id=_institution(session, "tuik").id,
            external_code=code,
            name="Sektör İstatistikleri",
            attributes={"default_frequency": "annual"},
        )
        session.add(dataset)
        session.flush()
        dimension = DatasetDimension(
            dataset_id=dataset.id, code="NACE", label="Sektör", position=0, role="other"
        )
        session.add(dimension)
        session.flush()
        session.add(
            DimensionCode(
                dimension_id=dimension.id,
                code="A",
                label="Tarım",
            )
        )
        session.add(
            DimensionCode(
                dimension_id=dimension.id,
                code="A1",
                label="Bitkisel üretim",
                parent_code="A",
                attributes={"unit": "Kişi"},
            )
        )
        session.commit()

        definition = build_series_definition(dataset, {"NACE": "A1"})
        assert definition.external_code == f"{code}:A1"
        assert definition.frequency == "annual"
        assert definition.unit == "Kişi"
        assert definition.breakdown["NACE"]["label"] == "Bitkisel üretim"
        assert "Bitkisel üretim" in definition.name
