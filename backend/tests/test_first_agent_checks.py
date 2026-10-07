"""Unit tests for the transform-fit and tautology checks (Task 3.3)."""

from __future__ import annotations

import pytest

from app.first_agent.checks import SeriesMeta, check_transform, find_tautology, same_family


def _meta(
    label: str = "S",
    *,
    measure: str | None = "endeks",
    nature: str | None = "gerceklesen",
    transforms: tuple[str, ...] | None = ("level", "percent_change_annual"),
    tags: frozenset[str] = frozenset(),
) -> SeriesMeta:
    return SeriesMeta(
        label=label,
        institution="tuik",
        dataset="D",
        measure_type=measure,
        data_nature=nature,
        transforms=transforms,
        leaf_tags=tags,
    )


def test_transform_fits_when_every_series_allows_it() -> None:
    error, verified = check_transform("annual_pct_change", [_meta("A"), _meta("B")])
    assert error is None
    assert verified is True


def test_transform_rejected_names_the_series_and_the_allowed_transforms() -> None:
    only_level = _meta("Aylık % değişim", transforms=("level",))
    error, _verified = check_transform("annual_pct_change", [_meta("A"), only_level])
    assert error is not None
    assert "Aylık % değişim" in error
    assert "level" in error


def test_difference_needs_only_the_level() -> None:
    error, _ = check_transform("difference", [_meta(transforms=("level",))])
    assert error is None


def test_unknown_measure_passes_but_is_not_verified() -> None:
    error, verified = check_transform("annual_pct_change", [_meta(transforms=None)])
    assert error is None
    assert verified is False


def test_unknown_transform_is_an_error() -> None:
    error, _ = check_transform("log_ratio", [_meta()])
    assert error is not None


def test_same_family_needs_shared_leaf_measure_and_nature() -> None:
    cpi = _meta("TÜFE", tags=frozenset({"tuketici_fiyatlari"}))
    rent = _meta("TÜFE kira", tags=frozenset({"tuketici_fiyatlari", "konut"}))
    assert same_family(cpi, rent) is not None


@pytest.mark.parametrize(
    ("other"),
    [
        _meta("Beklenti", nature="beklenti", tags=frozenset({"tuketici_fiyatlari"})),
        _meta("Kur", measure="fiyat_kur", tags=frozenset({"tuketici_fiyatlari"})),
        _meta("Başka konu", tags=frozenset({"issizlik"})),
        _meta("Etiketsiz", tags=frozenset()),
        _meta("Ölçüsüz", measure=None, tags=frozenset({"tuketici_fiyatlari"})),
    ],
)
def test_not_same_family(other: SeriesMeta) -> None:
    cpi = _meta("TÜFE", tags=frozenset({"tuketici_fiyatlari"}))
    assert same_family(cpi, other) is None


def test_find_tautology_checks_every_driver() -> None:
    target = _meta("Hedef", tags=frozenset({"a"}))
    drivers = [_meta("Kur", tags=frozenset({"b"})), _meta("Parça", tags=frozenset({"a"}))]
    reason = find_tautology(target, drivers)
    assert reason is not None
    assert "Parça" in reason
    assert find_tautology(target, drivers[:1]) is None
