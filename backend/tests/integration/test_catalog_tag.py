"""Integration test: Jev dataset tagging on a small seeded catalog (Task 2.3).

Written for the isolated ``karven-test`` stack; intentionally NOT run here. Run
with ``uv run python scripts/integration.py``.

The test DB is shared with every other test file, so each test:
- uses get-or-create institutions (never inserts a duplicate ``tuik``),
- owns unique dataset external codes (a uuid suffix) and deletes its rows in a
  fixture teardown,
- filters ``TagPass``/``refresh`` to its own datasets, never the whole DB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.catalog import tag
from app.catalog.tag_jev import TagPass
from app.catalog.tree import load_tree
from app.data.models import (
    CatalogLink,
    Dataset,
    DatasetDimension,
    DatasetTag,
    DimensionCode,
    Institution,
    MeasureCombination,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


class _FakeTagJev:
    """A dataset about population: the population branch and its leaves score high."""

    def __init__(self) -> None:
        tree = load_tree()
        self.branch_id = "nufus_demografi"
        self.leaves = set(tree.branch_leaves[self.branch_id])
        self.calls = 0

    def decide(self, state, questions, *, prompt_ref=None):  # noqa: ANN001
        self.calls += 1
        answers: dict[str, dict] = {}
        for question_id in questions:
            if question_id == self.branch_id:
                score = 0.92
            elif question_id in self.leaves:
                score = 0.82
            else:
                score = 0.03
            answers[question_id] = {"type": "noul", "noul": score}
        return {"answers": answers, "usage": {}, "provider": "fake", "model": "fake"}


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
        session.execute(
            sa.delete(MeasureCombination).where(MeasureCombination.dataset_id.in_(ids))
        )
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


def _seed_dataset(session, code: str, name: str = "Nüfus İstatistikleri") -> int:
    tuik = _institution(session, "tuik")
    dataset = Dataset(institution_id=tuik.id, external_code=code, name=name)
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
            attributes={"unit": "Kişi", "frequency": "annual"},
        )
    )
    session.commit()
    return dataset.id


def _dataset_id(session, code: str) -> int:
    return session.scalar(sa.select(Dataset.id).where(Dataset.external_code == code))


def _jev_tags(session, dataset_id: int) -> list[DatasetTag]:
    return list(
        session.scalars(
            sa.select(DatasetTag)
            .where(DatasetTag.dataset_id == dataset_id, DatasetTag.source == "jev")
            .order_by(DatasetTag.tag_id)
        ).all()
    )


def test_tag_pass_writes_then_force_rewrites(seeded_codes: list[str]) -> None:
    code = _unique("TAG")
    seeded_codes.append(code)
    fake = _FakeTagJev()

    with SessionLocal() as session:
        dataset_id = _seed_dataset(session, code)

    run = TagPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=1,
        dataset=code,
    )
    summary = run.run()
    assert summary.errors == 0
    assert not summary.aborted
    assert fake.calls >= 2  # branch stage + leaf stage

    with SessionLocal() as session:
        rows = _jev_tags(session, dataset_id)
        assert rows, "no Jev tags were written"
        accepted = [row for row in rows if row.status == "accepted"]
        assert accepted
        assert {row.tag_id for row in accepted} <= fake.leaves
        dataset = session.get(Dataset, dataset_id)
        tagging = dataset.attributes["tagging"]
        assert tagging["branch_scores"]
        assert tagging["leaf_scores"]
        assert tagging["thresholds"]["leaf_accept"] == tag.LEAF_ACCEPT
        assert tagging["prompt_versions"]["branch"]["version"] >= 1

    calls_before = fake.calls

    # A second plain run must not touch a dataset that already has a tagging attr.
    second = TagPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=1,
        dataset=code,
    )
    second.run()
    assert fake.calls == calls_before

    # --force deletes the Jev rows and rewrites them.
    forced = TagPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=1,
        dataset=code,
        force=True,
    )
    forced.run()
    assert fake.calls > calls_before
    with SessionLocal() as session:
        assert _jev_tags(session, dataset_id)


def test_manual_tag_survives_a_jev_run(seeded_codes: list[str]) -> None:
    code = _unique("MAN")
    seeded_codes.append(code)
    fake = _FakeTagJev()

    with SessionLocal() as session:
        dataset_id = _seed_dataset(session, code)
        dataset = session.get(Dataset, dataset_id)
        tag.set_manual_tags(session, dataset, leaves=["nufus"])
        session.commit()

    run = TagPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=1,
        dataset=code,
        force=True,
    )
    run.run()
    assert fake.calls == 0  # manual dataset is skipped entirely

    with SessionLocal() as session:
        rows = list(
            session.scalars(
                sa.select(DatasetTag).where(DatasetTag.dataset_id == dataset_id)
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].source == "manual"
        assert rows[0].tag_id == "nufus"
        assert rows[0].status == "accepted"


def test_refresh_recomputes_without_jev(seeded_codes: list[str]) -> None:
    code = _unique("REF")
    seeded_codes.append(code)
    fake = _FakeTagJev()

    with SessionLocal() as session:
        dataset_id = _seed_dataset(session, code)

    TagPass(
        session_factory=SessionLocal,
        jev_factory=lambda: fake,
        tree=load_tree(),
        workers=1,
        dataset=code,
    ).run()
    calls_before = fake.calls

    with SessionLocal() as session:
        counts = tag.refresh(session, dataset=code, leaf_accept=0.99)
        session.commit()
    assert counts["datasets"] == 1
    assert fake.calls == calls_before  # refresh never calls Jev

    with SessionLocal() as session:
        rows = _jev_tags(session, dataset_id)
        assert rows
        assert all(row.status == "review" for row in rows)
