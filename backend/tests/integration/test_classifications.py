"""Integration tests for the classification tables (isolated ``karven-test`` stack)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from app.connectors.tuik.__main__ import _cmd_siniflama
from app.connectors.tuik.siniflama import (
    NORMALIZATION,
    SOURCE,
    ClassificationData,
    ClassificationItemRow,
    CorrespondenceItemRow,
    CorrespondenceSummary,
    SiniflamaClient,
    SiniflamaVersion,
    link_dimensions,
    load_classifications,
    load_correspondences,
)
from app.data.models import (
    Classification,
    ClassificationCorrespondence,
    ClassificationCorrespondenceItem,
    ClassificationItem,
    Dataset,
    DatasetDimension,
    DimensionClassificationLink,
    DimensionCode,
    Institution,
)
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _items(count: int) -> list[ClassificationItemRow]:
    return [
        ClassificationItemRow(
            code=f"{index:04d}",
            parent_code=None,
            level=1,
            label=f"Item {index}",
            label_en=None,
        )
        for index in range(count)
    ]


def test_bulk_upsert_round_trip_and_removed_marking() -> None:
    version = SiniflamaVersion(external_id=_unique("vid"), type_code="99", name="Test version")
    items = _items(1500)
    with SessionLocal() as session:
        loads = load_classifications(session, [ClassificationData(version=version, items=items)])
        session.commit()

    assert loads[0].outcome == "inserted"
    assert loads[0].items_inserted == 1500

    with SessionLocal() as session:
        classification_id = session.scalar(
            sa.select(Classification.id).where(Classification.external_id == version.external_id)
        )
        stored = session.scalar(
            sa.select(sa.func.count())
            .select_from(ClassificationItem)
            .where(ClassificationItem.classification_id == classification_id)
        )
    assert stored == 1500

    # Re-run: drop one code and change the first item's label_en while keeping
    # its (code, parent_code, label) identity, then check the exact counts.
    changed = items[:-1]
    changed[0] = replace(changed[0], label_en="Renamed")
    with SessionLocal() as session:
        loads = load_classifications(session, [ClassificationData(version=version, items=changed)])
        session.commit()

    assert loads[0].items_updated == 1
    assert loads[0].items_unchanged == len(changed) - 1
    assert loads[0].items_removed == 1

    with SessionLocal() as session:
        removed = session.scalar(
            sa.select(ClassificationItem).where(
                ClassificationItem.classification_id == classification_id,
                ClassificationItem.code == items[-1].code,
            )
        )
        assert removed is not None
        assert removed.attributes.get("removed_at")


def test_parent_missing_flag_round_trips() -> None:
    version = SiniflamaVersion(external_id=_unique("vid"), type_code="99", name="Defect version")
    orf = ClassificationItemRow(
        code="15.11.15",
        parent_code=None,
        level=6,
        label="Koyun etleri",
        label_en=None,
        attributes={"parent_missing": True},
    )
    with SessionLocal() as session:
        load_classifications(session, [ClassificationData(version=version, items=[orf])])
        session.commit()

    with SessionLocal() as session:
        classification_id = session.scalar(
            sa.select(Classification.id).where(Classification.external_id == version.external_id)
        )
        row = session.scalar(
            sa.select(ClassificationItem).where(
                ClassificationItem.classification_id == classification_id,
                ClassificationItem.code == "15.11.15",
            )
        )
        assert row.parent_code is None
        assert row.attributes["parent_missing"] is True


def test_correspondence_fks_resolve_and_tolerate_missing_versions() -> None:
    from_version = SiniflamaVersion(external_id=_unique("from"), type_code="80", name="From")
    to_version = SiniflamaVersion(external_id=_unique("to"), type_code="80", name="To")
    with SessionLocal() as session:
        load_classifications(
            session,
            [
                ClassificationData(version=from_version, items=_items(2)),
                ClassificationData(version=to_version, items=_items(3)),
            ],
        )
        session.commit()

    linked = CorrespondenceSummary(
        external_id=_unique("corr"),
        name="Linked",
        from_external_id=from_version.external_id,
        to_external_id=to_version.external_id,
    )
    unlinked = CorrespondenceSummary(
        external_id=_unique("corr"),
        name="Ghost",
        from_external_id="999999999",
        to_external_id=to_version.external_id,
    )
    entries = [
        (
            linked,
            [
                CorrespondenceItemRow("0000", "0000", "Item 0", "Item 0"),
                CorrespondenceItemRow("-", "*", None, None),
            ],
        ),
        (unlinked, [CorrespondenceItemRow("0001", "0001", None, None)]),
    ]
    with SessionLocal() as session:
        loads = load_correspondences(session, entries)
        session.commit()

    by_id = {load.external_id: load for load in loads}
    assert by_id[linked.external_id].from_linked is True
    assert by_id[linked.external_id].to_linked is True
    assert by_id[unlinked.external_id].from_linked is False
    assert by_id[unlinked.external_id].to_linked is True

    with SessionLocal() as session:
        row = session.scalar(
            sa.select(ClassificationCorrespondence).where(
                ClassificationCorrespondence.external_id == linked.external_id
            )
        )
        assert row.from_classification_id is not None
        assert row.to_classification_id is not None
        assert row.attributes["from_external_id"] == from_version.external_id
        codes = {
            (item.from_code, item.to_code)
            for item in session.scalars(
                sa.select(ClassificationCorrespondenceItem).where(
                    ClassificationCorrespondenceItem.correspondence_id == row.id
                )
            )
        }
        assert ("-", "*") in codes
        assert (
            session.scalar(
                sa.select(Classification.source).where(
                    Classification.id == row.from_classification_id
                )
            )
            == SOURCE
        )


def test_identity_constraint_treats_null_parent_as_equal() -> None:
    version = SiniflamaVersion(external_id=_unique("vid"), type_code="99", name="Null parent")
    item_kwargs = {"code": "NULL-PARENT", "parent_code": None, "level": 2, "label": "Same label"}
    with SessionLocal() as session:
        classification = Classification(
            source=SOURCE,
            external_id=version.external_id,
            type_code=version.type_code,
            name=version.name,
        )
        session.add(classification)
        session.flush()
        classification_id = classification.id
        session.add(ClassificationItem(classification_id=classification_id, **item_kwargs))
        session.commit()

    with SessionLocal() as session:
        session.add(ClassificationItem(classification_id=classification_id, **item_kwargs))
        with pytest.raises(sa.exc.IntegrityError):
            session.commit()
        session.rollback()


def test_siniflama_streaming_commits_per_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetSiniflamalarSahibi"):
            if request.url.params.get("tur") == "1":
                return httpx.Response(
                    200,
                    json=[{"Id": 900101, "Ad": "Stream A"}, {"Id": 900102, "Ad": "Stream B"}],
                )
            return httpx.Response(200, json=[])
        if request.url.params.get("surumId") == "900101":
            return httpx.Response(
                200,
                json=[
                    {"kod": "A", "ust_kod": None, "duzey": 1, "tanim": "Alpha"},
                    {"kod": "A.1", "ust_kod": "A", "duzey": 2, "tanim": "Alpha one"},
                ],
            )
        # Version B is malformed: its parse fails after version A is committed.
        return httpx.Response(200, json={"not": "a tree"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = SiniflamaClient(http_client=http_client, store=None, sleeper=lambda _: None)
    args = SimpleNamespace(only_id=None, no_correspondences=True)
    try:
        with SessionLocal() as session:
            assert _cmd_siniflama(session, client, args) == 0
    finally:
        client.close()

    with SessionLocal() as session:
        first = session.scalar(
            sa.select(Classification).where(Classification.external_id == "900101")
        )
        second = session.scalar(
            sa.select(Classification).where(Classification.external_id == "900102")
        )
        assert first is not None
        assert second is None
        items = session.scalar(
            sa.select(sa.func.count())
            .select_from(ClassificationItem)
            .where(ClassificationItem.classification_id == first.id)
        )
    assert items == 2


def test_link_dimensions_writes_scores_and_is_rerunnable() -> None:
    version = SiniflamaVersion(external_id=_unique("vid"), type_code="98", name="Link version")
    items = [
        ClassificationItemRow("01", None, 1, "Toplam", "Total"),
        ClassificationItemRow("01.1", None, 2, "Et", "Meat"),
        ClassificationItemRow("01.1.1", None, 3, "Ekmek", "Cereals"),
    ]
    with SessionLocal() as session:
        load_classifications(session, [ClassificationData(version=version, items=items)])
        institution = Institution(code=_unique("inst"), name="Link test")
        session.add(institution)
        session.flush()
        dataset = Dataset(
            institution_id=institution.id,
            external_code=_unique("ds"),
            name="Link dataset",
            attributes={"channel": "databrowser2"},
        )
        session.add(dataset)
        session.flush()
        dimension = DatasetDimension(
            dataset_id=dataset.id, code="COICOP_2018", label="COICOP", position=1, role="other"
        )
        session.add(dimension)
        session.flush()
        for code, label in (
            ("01", "Total"),
            ("011", "Meat"),
            ("0111", "Cereals"),
            ("TOTAL", "aggregate ignored"),
        ):
            session.add(DimensionCode(dimension_id=dimension.id, code=code, label=label))
        session.commit()
        dimension_id = dimension.id
        dataset_code = dataset.external_code
        classification_id = session.scalar(
            sa.select(Classification.id).where(Classification.external_id == version.external_id)
        )

    def load_link() -> DimensionClassificationLink | None:
        with SessionLocal() as session:
            return session.scalar(
                sa.select(DimensionClassificationLink).where(
                    DimensionClassificationLink.dimension_id == dimension_id,
                    DimensionClassificationLink.classification_id == classification_id,
                )
            )

    with SessionLocal() as session:
        result = link_dimensions(session, collect_report=True)
        session.commit()

    link = load_link()
    assert link is not None
    assert link.matched_codes == 3
    assert link.total_codes == 3
    assert link.coverage == 1.0
    assert link.label_agreement == 1.0
    assert link.attributes["normalization"] == NORMALIZATION
    assert any(
        row.qualified
        and row.dataset == dataset_code
        and row.dimension == "COICOP_2018"
        and row.total == 3
        for row in result.report
    )

    # Re-run: the same link is unchanged, not duplicated.
    with SessionLocal() as session:
        rerun = link_dimensions(session)
        session.commit()
    assert rerun.unchanged >= 1
    assert load_link() is not None

    # A code the version does not contain drops coverage below the threshold and
    # the stale link is deleted by the next run.
    with SessionLocal() as session:
        session.add(DimensionCode(dimension_id=dimension_id, code="ZZ9", label="Unknown"))
        session.commit()

    with SessionLocal() as session:
        deleted = link_dimensions(session)
        session.commit()
    assert deleted.deleted >= 1
    assert load_link() is None
