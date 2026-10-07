import pytest

from app.graph.errors import InvalidRelationError
from app.graph.keys import relation_key, series_key
from app.graph.models import (
    RELATION_STATUSES,
    Hypothesis,
    PeriodResult,
    SeriesRef,
    normalize_relation,
    validate_driver_directions,
    validate_nominal_members,
    validate_status,
)


def test_series_key_format() -> None:
    assert series_key("TCMB", "TP.FG.J0") == "TCMB|TP.FG.J0"


def test_series_key_rejects_empty_parts() -> None:
    with pytest.raises(InvalidRelationError):
        series_key("", "X")
    with pytest.raises(InvalidRelationError):
        series_key("TCMB", "")


def test_series_key_separator_is_safe() -> None:
    # The two parts must not be confusable: a "|" inside a part is rejected.
    with pytest.raises(InvalidRelationError):
        series_key("A|B", "C")
    with pytest.raises(InvalidRelationError):
        series_key("A", "B|C")


def test_series_ref_key_and_validation() -> None:
    ref = SeriesRef(institution="TCMB", code="TP.FG.J0")

    assert ref.key == "TCMB|TP.FG.J0"
    with pytest.raises(InvalidRelationError):
        SeriesRef(institution="A|B", code="C")


def test_relation_key_driver_order_is_irrelevant() -> None:
    assert relation_key("t", ["a", "b"]) == relation_key("t", ["b", "a"])


def test_relation_key_collapses_duplicate_drivers() -> None:
    assert relation_key("t", ["a", "a", "b"]) == relation_key("t", ["a", "b"])


def test_relation_key_reverse_direction_differs() -> None:
    assert relation_key("a", ["b"]) != relation_key("b", ["a"])


def test_relation_key_rejects_blank_driver() -> None:
    with pytest.raises(InvalidRelationError):
        relation_key("t", ["a", ""])


def test_hypothesis_accepts_valid_values() -> None:
    hypothesis = Hypothesis(
        mechanism="m",
        driver_directions={"a": "positive", "b": "negative"},
        lag_min=0,
        lag_max=3,
        transform="difference",
        nominal_tl_series=("a",),
    )

    assert hypothesis.driver_directions == {"a": "positive", "b": "negative"}
    assert hypothesis.nominal_tl_series == ("a",)


def test_hypothesis_rejects_bad_mechanism() -> None:
    with pytest.raises(InvalidRelationError):
        Hypothesis("", {"a": "positive"}, 0, 1, "difference")


def test_hypothesis_rejects_empty_driver_directions() -> None:
    with pytest.raises(InvalidRelationError):
        Hypothesis("m", {}, 0, 1, "difference")


def test_hypothesis_rejects_bad_direction() -> None:
    with pytest.raises(InvalidRelationError):
        Hypothesis("m", {"a": "up"}, 0, 1, "difference")


def test_hypothesis_rejects_bad_transform() -> None:
    with pytest.raises(InvalidRelationError):
        Hypothesis("m", {"a": "positive"}, 0, 1, "log")


def test_hypothesis_rejects_bad_lag_order() -> None:
    with pytest.raises(InvalidRelationError):
        Hypothesis("m", {"a": "positive"}, 3, 1, "difference")


def test_hypothesis_rejects_negative_lag() -> None:
    with pytest.raises(InvalidRelationError):
        Hypothesis("m", {"a": "positive"}, -1, 1, "difference")


def test_period_result_validation() -> None:
    assert PeriodResult("all_years", "supported").period == "all_years"
    with pytest.raises(InvalidRelationError):
        PeriodResult("last_5_years", "supported")
    with pytest.raises(InvalidRelationError):
        PeriodResult("all_years", "maybe")
    with pytest.raises(InvalidRelationError):
        PeriodResult("all_years", "supported", details=123)  # type: ignore[arg-type]


def test_period_result_reliability_validation() -> None:
    # reliable=False requires a non-empty reason.
    with pytest.raises(InvalidRelationError):
        PeriodResult("all_years", "supported", reliable=False)
    with pytest.raises(InvalidRelationError):
        PeriodResult("all_years", "supported", reliable=False, reliability_reason="")
    # reliable=True must carry no reason.
    with pytest.raises(InvalidRelationError):
        PeriodResult("all_years", "supported", reliable=True, reliability_reason="why")

    unreliable = PeriodResult(
        "all_years", "supported", reliable=False, reliability_reason="small effective sample"
    )
    assert unreliable.reliable is False
    assert unreliable.reliability_reason == "small effective sample"
    assert PeriodResult("all_years", "supported").reliable is True


def test_normalize_relation_rejects_without_drivers() -> None:
    with pytest.raises(InvalidRelationError):
        normalize_relation(SeriesRef("T", "X"), [])


def test_normalize_relation_rejects_target_as_driver() -> None:
    target = SeriesRef("T", "X")
    with pytest.raises(InvalidRelationError):
        normalize_relation(target, [target])


def test_normalize_relation_collapses_duplicate_drivers() -> None:
    target = SeriesRef("T", "T")
    driver = SeriesRef("T", "D")

    _, drivers = normalize_relation(target, [driver, driver])

    assert drivers == (driver,)


def test_normalize_relation_rejects_non_series_driver() -> None:
    with pytest.raises(InvalidRelationError):
        normalize_relation(SeriesRef("T", "T"), ["not-a-ref"])


def test_validate_nominal_members_subset() -> None:
    target = SeriesRef("T", "T")
    driver = SeriesRef("T", "D")
    hypothesis = Hypothesis("m", {driver.key: "positive"}, 0, 1, "difference", (target.key,))
    validate_nominal_members(hypothesis, target, (driver,))

    with pytest.raises(InvalidRelationError):
        validate_nominal_members(
            Hypothesis("m", {driver.key: "positive"}, 0, 1, "difference", ("other|key",)),
            target,
            (driver,),
        )


def test_validate_driver_directions_requires_exact_driver_set() -> None:
    driver_a = SeriesRef("T", "A")
    driver_b = SeriesRef("T", "B")
    good = Hypothesis("m", {driver_a.key: "positive", driver_b.key: "negative"}, 0, 1, "difference")
    validate_driver_directions(good, (driver_a, driver_b))

    missing = Hypothesis("m", {driver_a.key: "positive"}, 0, 1, "difference")
    with pytest.raises(InvalidRelationError):
        validate_driver_directions(missing, (driver_a, driver_b))

    extra = Hypothesis(
        "m",
        {driver_a.key: "positive", driver_b.key: "negative", "T|C": "positive"},
        0,
        1,
        "difference",
    )
    with pytest.raises(InvalidRelationError):
        validate_driver_directions(extra, (driver_a, driver_b))


def test_validate_status_accepts_closed_set() -> None:
    for status in RELATION_STATUSES:
        validate_status(status)

    # "Rejected" is unsupported; it is not a separate state.
    with pytest.raises(InvalidRelationError):
        validate_status("rejected")
