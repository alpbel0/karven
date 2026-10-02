"""Integration tests for the Veri Portalı catalogue, press catalogue and links."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.connectors.base import (
    DatasetMeta,
    DimensionCodeMeta,
    DimensionMeta,
    upsert_dataset,
    upsert_institution,
)
from app.connectors.tuik.veriportali import (
    PressItem,
    load_press_catalog,
    sync_veriportali_catalog,
)
from app.data.models import Dataset, DatasetDimension, Document, DocumentDatasetLink
from app.db.session import SessionLocal

pytestmark = pytest.mark.integration


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _institution(session, code):
    return upsert_institution(session, code, code)


def test_veriportali_merge_keeps_existing_dimensions_and_creates_new() -> None:
    code = _unique("inst")
    external = _unique("DF_PORTAL")
    with SessionLocal() as session:
        institution = _institution(session, code)
        # An existing databrowser2-style dataset with real dimensions and codes.
        _, dataset = upsert_dataset(
            session,
            institution.id,
            DatasetMeta(
                external_code=external,
                name="Original",
                attributes={"channel": "databrowser2", "version": "1.0"},
                dimensions=[
                    DimensionMeta(
                        code="REF_AREA",
                        label="Reference area",
                        position=0,
                        role="geo",
                        codes=[DimensionCodeMeta(code="TR", label="Türkiye")],
                    )
                ],
            ),
        )
        session.commit()
        dataset_id = dataset.id
        dimension_count = session.scalar(
            sa.select(sa.func.count())
            .select_from(DatasetDimension)
            .where(DatasetDimension.dataset_id == dataset_id)
        )

    new_code = _unique("DF_NEW")
    with SessionLocal() as session:
        institution_id = session.scalar(
            sa.select(Dataset.institution_id).where(Dataset.id == dataset_id)
        )
        records = [
            _record(external, version="1.0"),
            _record(new_code, version="1.0"),
        ]
        result = sync_veriportali_catalog(session, records, institution_id=institution_id)
        session.commit()
        assert result.created == 1
        assert result.updated == 1

    with SessionLocal() as session:
        existing = session.get(Dataset, dataset_id)
        assert existing is not None
        assert existing.dimensions
        assert existing.attributes["veriportali"]["id"]
        assert existing.attributes["channel"] == "databrowser2"
        dims = session.scalar(
            sa.select(sa.func.count())
            .select_from(DatasetDimension)
            .where(DatasetDimension.dataset_id == dataset_id)
        )
        assert dims == dimension_count  # dimensions untouched
        new = session.scalar(
            sa.select(Dataset).where(
                Dataset.institution_id == institution_id,
                Dataset.external_code == new_code,
            )
        )
        assert new is not None
        assert new.dimensions == []

    # A databrowser2-style upsert after the portal merge must keep veriportali.
    with SessionLocal() as session:
        existing = session.get(Dataset, dataset_id)
        prior = existing.attributes["veriportali"]
        upsert_dataset(
            session,
            institution_id,
            DatasetMeta(
                external_code=external,
                name="Renamed by databrowser2",
                attributes={"channel": "databrowser2", "version": "1.0"},
                dimensions=[
                    DimensionMeta(
                        code="REF_AREA",
                        label="Reference area",
                        position=0,
                        role="geo",
                        codes=[DimensionCodeMeta(code="TR", label="Türkiye")],
                    )
                ],
            ),
        )
        session.commit()
    with SessionLocal() as session:
        existing = session.get(Dataset, dataset_id)
        assert existing.attributes["channel"] == "databrowser2"
        assert existing.attributes["veriportali"] == prior


def test_veriportali_removal_only_on_complete_and_reappearance_clears() -> None:
    code = _unique("inst")
    external = _unique("DF_PORTAL")
    with SessionLocal() as session:
        institution = _institution(session, code)
        sync_veriportali_catalog(
            session, [_record(external)], institution_id=institution.id, complete=True
        )
        session.commit()
        institution_id = institution.id

    # A complete list that omits the record marks it removed.
    with SessionLocal() as session:
        result = sync_veriportali_catalog(session, [], institution_id=institution_id, complete=True)
        session.commit()
        assert result.removed == 1

    with SessionLocal() as session:
        dataset = session.scalar(
            sa.select(Dataset).where(
                Dataset.institution_id == institution_id,
                Dataset.external_code == external,
            )
        )
        assert dataset.attributes["veriportali"]["removed_at"]

    # A partial crawl never marks anything.
    with SessionLocal() as session:
        result = sync_veriportali_catalog(
            session, [], institution_id=institution_id, complete=False
        )
        assert result.removed == 0

    # Reappearance clears the marker.
    with SessionLocal() as session:
        sync_veriportali_catalog(
            session, [_record(external)], institution_id=institution_id, complete=True
        )
        session.commit()
    with SessionLocal() as session:
        dataset = session.scalar(
            sa.select(Dataset).where(
                Dataset.institution_id == institution_id,
                Dataset.external_code == external,
            )
        )
        assert "removed_at" not in dataset.attributes["veriportali"]


def test_press_catalog_idempotent_and_removal_only_when_complete() -> None:
    from app.connectors.tuik.veriportali import SOURCE

    code = _unique("inst")
    with SessionLocal() as session:
        institution = _institution(session, code)
        first = load_press_catalog(
            session, [_press("7001"), _press("7002")], institution_id=institution.id
        )
        session.commit()
        institution_id = institution.id
    assert (first.inserted, first.updated, first.unchanged, first.removed) == (2, 0, 0, 0)

    with SessionLocal() as session:
        again = load_press_catalog(
            session, [_press("7001"), _press("7002")], institution_id=institution_id
        )
        session.commit()
    assert (again.inserted, again.updated, again.unchanged, again.removed) == (0, 0, 2, 0)

    with SessionLocal() as session:
        partial = load_press_catalog(
            session, [_press("7001")], institution_id=institution_id, complete=False
        )
        session.commit()
    assert partial.removed == 0

    with SessionLocal() as session:
        complete = load_press_catalog(
            session, [_press("7001")], institution_id=institution_id, complete=True
        )
        session.commit()
    assert complete.removed == 1
    with SessionLocal() as session:
        removed = session.scalar(
            sa.select(Document).where(Document.source == SOURCE, Document.external_id == "7002")
        )
        assert removed is not None and removed.attributes.get("removed_at")


def test_press_fetch_writes_text_links_and_resolves_dataset_id() -> None:
    from app.connectors.tuik.veriportali import SOURCE, fetch_press_release

    code = _unique("inst")
    known_code = _unique("DF_KNOWN")
    missing_code = _unique("DF_MISSING")
    press_id = _unique("press")
    with SessionLocal() as session:
        institution = _institution(session, code)
        _, dataset = upsert_dataset(
            session,
            institution.id,
            DatasetMeta(external_code=known_code, name="Known", attributes={"channel": "test"}),
        )
        session.commit()
        institution_id = institution.id
        dataset_id = dataset.id

    detail = {
        "data": {
            "id": press_id,
            "title": "Test Bülteni",
            "period": "Ağustos 2026",
            "date": "2026-09-03T10:00:00",
            "contactEmail": "a@b.c",
            "content": "<p>Birinci</p><br><br><p>İkinci &amp; üçüncü</p>",
            "statisticalTables": [
                {
                    "type": "dataflow",
                    "title": "T1",
                    "url": f"https://databrowser2.tuik.gov.tr/#/tr/tuik/categories/6/6_5/TR,{known_code},1.0",
                },
                {
                    "type": "dataflow",
                    "title": "T2",
                    "url": f"https://databrowser2.tuik.gov.tr/#/tr/tuik/categories/6/6_5/TR,{missing_code},1.0",
                },
            ],
            "tables": [{"type": "xls", "title": "x", "url": "/api/tr/data/downloads?t=t"}],
            "reports": [{"type": "pdf", "title": "r", "url": "/api/tr/data/downloads?t=r"}],
            "previousPresses": [{"title": "Old", "period": "Temmuz", "url": "/tr/press/1"}],
            "metadatas": [{"big": "value"}],
        },
        "isError": False,
    }

    class _FakeClient:
        base_url = "https://veriportali.tuik.gov.tr"

        class _Raw:
            raw_object_key = "sources/tuik/press/x.json"

            def json(self):
                return detail

        def press_detail(self, press_id_arg):
            return self._Raw()

    with SessionLocal() as session:
        result = fetch_press_release(
            session, _FakeClient(), press_id, institution_id=institution_id
        )
        session.commit()
        assert result.created is True
        assert result.content_length > 0
        assert result.links_written == 2
        assert result.links_resolved == 1
        document_id = result.document_id

    with SessionLocal() as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.source == SOURCE
        assert document.content_text == "Birinci\n\nİkinci & üçüncü"
        assert document.attributes["press"]["raw_object_key"].endswith(".json")
        assert "metadatas" not in document.attributes["press"]
        links = {
            row.dataset_code: row
            for row in session.scalars(
                sa.select(DocumentDatasetLink).where(DocumentDatasetLink.document_id == document_id)
            )
        }
        assert links[known_code].dataset_id == dataset_id
        assert links[missing_code].dataset_id is None

    # Re-resolve: catalogue the missing dataset, fetch again, the link fills in.
    with SessionLocal() as session:
        institution_id = session.scalar(
            sa.select(Dataset.institution_id).where(Dataset.id == dataset_id)
        )
        upsert_dataset(
            session,
            institution_id,
            DatasetMeta(external_code=missing_code, name="Now known"),
        )
        session.commit()
    with SessionLocal() as session:
        result = fetch_press_release(
            session, _FakeClient(), press_id, institution_id=institution_id
        )
        session.commit()
        assert result.created is False  # idempotent
        assert result.links_resolved == 2
    with SessionLocal() as session:
        link = session.scalar(
            sa.select(DocumentDatasetLink).where(
                DocumentDatasetLink.document_id == document_id,
                DocumentDatasetLink.dataset_code == missing_code,
            )
        )
        assert link.dataset_id is not None

    # A later catalogue load (same id) keeps the on-demand press details and text.
    with SessionLocal() as session:
        outcome = load_press_catalog(
            session, [_press(press_id)], institution_id=institution_id, complete=False
        )
        session.commit()
        assert outcome.updated == 1
    with SessionLocal() as session:
        document = session.get(Document, document_id)
        assert document.attributes["press"]["raw_object_key"].endswith(".json")
        assert document.attributes["period"] == "Kasım 2025"
        assert document.content_text == "Birinci\n\nİkinci & üçüncü"
    with SessionLocal() as session:
        again = load_press_catalog(
            session, [_press(press_id)], institution_id=institution_id, complete=False
        )
        session.commit()
        assert again.unchanged == 1


# --- helpers ---------------------------------------------------------------


def _record(dataset_code: str, *, version: str = "1.0"):
    from app.connectors.tuik.veriportali import DataflowRecord

    return DataflowRecord(
        portal_id=f"{dataset_code}+V{version}",
        external_code=dataset_code,
        version=version,
        name=f"Name {dataset_code}",
        description="desc",
        period="2024",
        updated_at="2026-06-01T22:00:00",
        downloadable=True,
        category_path=["Cat", "Child"],
        footnotes=[],
    )


def _press(external_id: str) -> PressItem:
    return PressItem(
        external_id=external_id,
        title=f"Title {external_id}",
        subject="Kategori",
        doc_type="Haber Bülteni",
        year=2025,
        published_at=date(2025, 12, 31),
        url=f"https://veriportali.tuik.gov.tr/tr/press/{external_id}",
        attributes={"period": "Kasım 2025", "published_at_raw": "2025-12-31T10:00:00"},
    )
