"""Unit tests for catalog linking (fake Jev, no network, no database)."""

from __future__ import annotations

from typing import Any

import pytest

from app.catalog.linking import (
    MAX_CHOICE_OPTIONS,
    METHOD_JEV,
    RELATION_RELATED,
    RELATION_SAME_SERIES,
    STATUS_ACCEPTED,
    STATUS_PROPOSED,
    LinkingError,
    choice_question,
    indicator_codes,
    narrow_choice,
    parse_choice,
    parse_score,
    propose_links,
    select_dimension_code,
    series_full_name,
    upsert_link,
)
from app.data.models import CatalogLink, Dataset, DatasetDimension, DimensionCode


class FakeJev:
    """Records every question and answers choice/noul deterministically."""

    def __init__(self, pick: str | None = None, score: float = 0.9) -> None:
        self.pick = pick
        self.score = score
        self.questions: list[dict[str, Any]] = []

    def decide(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        prompt_ref: Any = None,
    ) -> dict[str, Any]:
        ((qid, question),) = questions.items()
        self.questions.append(question)
        if question["type"] == "choice":
            options = list(question["criteria"])
            index = 0
            if self.pick is not None:
                index = next((i for i, option in enumerate(options) if self.pick in option), 0)
            return {"answers": {qid: {"choice": options[index], "confidence": 0.9}}}
        return {"answers": {qid: {"noul": self.score, "confidence": 0.9}}}


class FakeSession:
    """Minimal session double for the link store (no SQL execution)."""

    def __init__(self, existing: CatalogLink | None = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.flushes = 0

    def scalar(self, statement: Any) -> CatalogLink | None:
        return self.existing

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, CatalogLink) and self.existing is None:
            self.existing = obj

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        return None


def _from_dataset() -> Dataset:
    dataset = Dataset(id=1, institution_id=1, external_code="TURCAT_REEL", name="Turcat Reel")
    dataset.dimensions = [
        DatasetDimension(
            code="INDICATOR",
            label="Gösterge",
            position=0,
            role="other",
            codes=[
                DimensionCode(
                    id=2,
                    code="2",
                    label="GSYH (cari fiyat)",
                    attributes={"unit": "TL Bin", "frequency": "quarterly"},
                ),
                DimensionCode(id=1, code="1", label="Ulusal Hesaplar", attributes={"group": True}),
            ],
        )
    ]
    return dataset


def _target_dataset() -> Dataset:
    dataset = Dataset(
        id=10,
        institution_id=1,
        external_code="DF_TUFE",
        name="Tüketici Fiyat Endeksi",
        source_category="Fiyat İstatistikleri / TÜFE",
    )
    dataset.dimensions = [
        DatasetDimension(
            code="REF_AREA",
            label="Bölge",
            position=0,
            role="geo",
            codes=[DimensionCode(code="TR", label="Türkiye")],
        ),
        DatasetDimension(
            code="FREQ",
            label="Frekans",
            position=1,
            role="frequency",
            codes=[DimensionCode(code="M", label="Aylık")],
        ),
    ]
    return dataset


def test_choice_question_enforces_option_limit() -> None:
    with pytest.raises(LinkingError):
        choice_question("pick", [])
    with pytest.raises(LinkingError):
        choice_question("pick", [str(i) for i in range(MAX_CHOICE_OPTIONS + 1)])
    with pytest.raises(LinkingError):
        choice_question("pick", ["a", "a"])
    question = choice_question("pick", ["a", "b"])
    assert question["type"] == "choice"
    assert question["criteria"] == {"a": "", "b": ""}


def test_parse_choice_accepts_index_label_and_numeric_text() -> None:
    options = ["Ankara", "Bursa", "Kayseri"]
    assert parse_choice({"choice": 1}, options).label == "Bursa"
    assert parse_choice({"choice": "Kayseri"}, options).index == 2
    assert parse_choice({"selected": "1"}, options).index == 1
    assert parse_choice({"choice": "bursa"}, options).index == 1
    with pytest.raises(LinkingError):
        parse_choice({"choice": 9}, options)
    with pytest.raises(LinkingError):
        parse_choice({}, options)


def test_parse_score_requires_a_number() -> None:
    assert parse_score({"score": 0.8, "confidence": 0.5}) == (0.8, 0.5)
    assert parse_score({"noul": 0.87})[0] == 0.87
    assert parse_score({"probability": "0.25"})[0] == 0.25
    with pytest.raises(LinkingError):
        parse_score({"other": 1})


def test_narrow_choice_chunks_every_question_to_at_most_255() -> None:
    jev = FakeJev()
    items = [f"item-{index}" for index in range(600)]

    chosen, _confidence = narrow_choice(jev, {}, "pick", items, labeler=str)

    assert chosen == "item-0"
    assert len(jev.questions) >= 2
    assert all(len(question["criteria"]) <= MAX_CHOICE_OPTIONS for question in jev.questions)
    assert all(question["type"] == "choice" for question in jev.questions)


def test_select_dimension_code_descends_parent_level_first() -> None:
    jev = FakeJev(pick="A1")
    dimension = DatasetDimension(
        code="NACE",
        label="Sektör",
        position=0,
        role="other",
        codes=[
            DimensionCode(code="A", label="Tarım"),
            DimensionCode(code="A1", label="Bitkisel üretim", parent_code="A"),
            DimensionCode(code="B", label="Sanayi"),
        ],
    )

    code, confidence, label = select_dimension_code(jev, {}, "request", dimension)

    assert code == "A1"
    assert label == "Bitkisel üretim"
    assert confidence == 0.9
    # First question offers roots only (A, B); the second offers A's children.
    assert len(jev.questions) == 2
    assert "Tarım [A]" in jev.questions[0]["criteria"]
    assert "Bitkisel üretim [A1]" in jev.questions[1]["criteria"]


def test_indicator_codes_skips_groups() -> None:
    codes = indicator_codes(_from_dataset())
    assert [code.code for code in codes] == ["2"]


def test_indicator_request_and_series_full_name() -> None:
    dataset = _from_dataset()
    code = indicator_codes(dataset)[0]
    request = f"{code.label} | birim: TL Bin | frekans: quarterly — {dataset.name}"
    assert "GSYH" in request
    assert series_full_name(_target_dataset(), {"REF_AREA": "TR", "FREQ": "M"}) == (
        "Tüketici Fiyat Endeksi — Bölge: Türkiye; Frekans: Aylık"
    )


def test_propose_links_persists_same_series_and_is_idempotent() -> None:
    session = FakeSession()
    jev = FakeJev()
    from_dataset = _from_dataset()
    targets = [_target_dataset()]

    first = propose_links(session, from_dataset, jev, targets=targets)
    second = propose_links(session, from_dataset, jev, targets=targets)

    assert len(first) == 1
    assert first[0].relation == RELATION_SAME_SERIES
    assert first[0].confidence == 0.9
    assert first[0].to_codes == {"REF_AREA": "TR", "FREQ": "M"}
    assert first[0].persisted is True
    links = [obj for obj in session.added if isinstance(obj, CatalogLink)]
    assert len(links) == 1
    assert links[0].method == METHOD_JEV
    assert links[0].status == STATUS_PROPOSED
    assert second[0].persisted is True
    assert len([obj for obj in session.added if isinstance(obj, CatalogLink)]) == 1


def test_propose_links_gates_on_verification_score() -> None:
    session = FakeSession()
    targets = [_target_dataset()]

    related = propose_links(session, _from_dataset(), FakeJev(score=0.5), targets=targets)
    assert related[0].relation == RELATION_RELATED
    assert related[0].status == STATUS_PROPOSED
    assert related[0].note and "doğrulama" in related[0].note

    skipped = propose_links(session, _from_dataset(), FakeJev(score=0.1), targets=targets)
    assert skipped[0].status == "skipped"
    assert skipped[0].persisted is False


def test_propose_links_dry_run_writes_nothing() -> None:
    session = FakeSession()
    proposals = propose_links(
        session, _from_dataset(), FakeJev(), targets=[_target_dataset()], dry_run=True
    )
    assert proposals[0].persisted is False
    assert [obj for obj in session.added if isinstance(obj, CatalogLink)] == []


def test_upsert_link_never_overwrites_a_review_decision() -> None:
    existing = CatalogLink(
        id=5,
        from_dataset_id=1,
        from_codes={"INDICATOR": "2"},
        to_dataset_id=10,
        to_codes={"REF_AREA": "TR", "FREQ": "M"},
        relation=RELATION_SAME_SERIES,
        method=METHOD_JEV,
        confidence=0.9,
        status=STATUS_ACCEPTED,
    )
    session = FakeSession(existing=existing)

    returned = upsert_link(
        session,
        1,
        {"INDICATOR": "2"},
        10,
        {"REF_AREA": "TR", "FREQ": "M"},
        relation=RELATION_RELATED,
        method=METHOD_JEV,
        confidence=0.1,
    )

    assert returned is existing
    assert existing.relation == RELATION_SAME_SERIES
    assert existing.confidence == 0.9
    assert session.added == []
