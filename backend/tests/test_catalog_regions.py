"""Unit tests for the region crosswalk builder (no database, no network)."""

from __future__ import annotations

from app.catalog.regions import (
    MANUAL_OVERRIDES,
    METHOD_LABEL_MATCH,
    METHOD_MANUAL,
    CrosswalkRow,
    IbbsProvince,
    ProvinceCode,
    is_province_plate,
    match_crosswalk,
    turkish_fold,
    validate_crosswalk,
)


def test_turkish_fold_handles_dotted_and_dotless_i() -> None:
    assert turkish_fold("İSTANBUL") == "istanbul"
    assert turkish_fold("İstanbul") == "istanbul"
    assert turkish_fold("KIRKLARELİ") == "kırklareli"
    assert turkish_fold("Kırklareli") == "kırklareli"


def test_turkish_fold_keeps_kayseri_spelling_apart() -> None:
    # The whole reason the manual override exists: CİP's "KAYSERI" does not fold
    # to the İBBS "Kayseri" spelling.
    assert turkish_fold("KAYSERI") == "kayserı"
    assert turkish_fold("Kayseri") == "kayseri"
    assert turkish_fold("KAYSERI") != turkish_fold("Kayseri")


def test_is_province_plate_window() -> None:
    assert is_province_plate("1")
    assert is_province_plate("81")
    assert not is_province_plate("0")
    assert not is_province_plate("82")
    assert not is_province_plate("TR1")
    assert not is_province_plate("1486")


def test_match_crosswalk_matches_by_folded_label_and_checks_parent() -> None:
    cip = [
        ProvinceCode(code="39", label="KIRKLARELİ", parent_code="TR21"),
        ProvinceCode(code="6", label="ANKARA", parent_code="TR51"),
    ]
    ibbs = [
        IbbsProvince(code="TR213", label="Kırklareli"),
        IbbsProvince(code="TR510", label="Ankara"),
    ]

    plan = match_crosswalk(cip, ibbs, overrides={})

    assert plan.ok
    rows = {row.from_code: row for row in plan.rows}
    assert list(rows) == ["6", "39"]  # sorted by plate number
    assert rows["6"].to_code == "TR510"
    assert rows["6"].method == METHOD_LABEL_MATCH
    assert rows["6"].label == "Ankara"
    assert rows["39"].to_code == "TR213"
    assert rows["39"].note is None


def test_match_crosswalk_rejects_a_bad_parent() -> None:
    cip = [ProvinceCode(code="6", label="ANKARA", parent_code="TR51")]
    ibbs = [IbbsProvince(code="TR213", label="Ankara")]

    plan = match_crosswalk(cip, ibbs, overrides={})

    assert not plan.ok
    assert plan.rows == ()
    assert "TR51" in plan.problems[0]
    assert "TR213" in plan.problems[0]


def test_match_crosswalk_reports_an_unmatched_label() -> None:
    cip = [ProvinceCode(code="99", label="NOWHERE", parent_code="TR21")]
    ibbs = [IbbsProvince(code="TR213", label="Kırklareli")]

    plan = match_crosswalk(cip, ibbs, overrides={})

    assert not plan.ok
    assert plan.rows == ()
    assert "NOWHERE" in plan.problems[0]


def test_manual_override_is_applied_with_a_note() -> None:
    cip = [ProvinceCode(code="38", label="KAYSERI", parent_code="TR72")]
    ibbs = [IbbsProvince(code="TR721", label="Kayseri")]

    without = match_crosswalk(cip, ibbs, overrides={})
    assert not without.ok  # folding alone cannot match KAYSERI

    with_override = match_crosswalk(cip, ibbs)  # default MANUAL_OVERRIDES
    assert with_override.ok
    (row,) = with_override.rows
    assert row.to_code == "TR721"
    assert row.method == METHOD_MANUAL
    assert row.label == "Kayseri"
    assert row.note and "KAYSERI" in row.note
    assert "38" in MANUAL_OVERRIDES


def test_validate_crosswalk_refuses_when_not_81_to_81() -> None:
    rows = match_crosswalk(
        [
            ProvinceCode(code="39", label="KIRKLARELİ", parent_code="TR21"),
            ProvinceCode(code="6", label="ANKARA", parent_code="TR51"),
        ],
        [
            IbbsProvince(code="TR213", label="Kırklareli"),
            IbbsProvince(code="TR510", label="Ankara"),
        ],
        overrides={},
    ).rows

    problems = validate_crosswalk(rows)

    assert any("expected 81" in problem for problem in problems)


def test_validate_crosswalk_rejects_duplicate_targets() -> None:
    def row(from_code: str) -> CrosswalkRow:
        return CrosswalkRow(
            from_scheme="cip_plate",
            from_code=from_code,
            to_scheme="ibbs",
            to_code="TR510",
            level="province",
            label="Ankara",
            method=METHOD_LABEL_MATCH,
        )

    problems = validate_crosswalk([row("6"), row("7")], expected=2)

    assert any("1:1" in problem for problem in problems)
    assert any("distinct İBBS" in problem for problem in problems)
