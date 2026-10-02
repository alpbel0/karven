"""Unit tests for observation value comparison and fetched_at validation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.data.observations import ensure_timezone_aware, to_decimal, values_equal


def test_decimal_equality_ignores_scale() -> None:
    assert values_equal(Decimal("1.50"), Decimal("1.5"))
    assert values_equal(1, Decimal("1.0"))
    assert values_equal(1.5, Decimal("1.5"))


def test_changed_values_differ() -> None:
    assert not values_equal(Decimal("1.5"), Decimal("1.6"))
    assert not values_equal(Decimal("1.5"), None)
    assert not values_equal(None, Decimal("1.5"))


def test_none_equals_none() -> None:
    assert values_equal(None, None)


def test_to_decimal_parses_strings_numbers() -> None:
    assert to_decimal("1.50") == Decimal("1.5")
    assert to_decimal(2) == Decimal(2)


def test_boolean_is_rejected() -> None:
    with pytest.raises(TypeError):
        to_decimal(True)


def test_naive_fetched_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        ensure_timezone_aware(datetime(2024, 10, 1))


def test_aware_fetched_at_is_accepted() -> None:
    value = datetime(2024, 10, 1, tzinfo=UTC)
    assert ensure_timezone_aware(value) is value
