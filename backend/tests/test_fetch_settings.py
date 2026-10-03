"""Unit tests for the on-demand fetch settings validation."""

from __future__ import annotations

import pytest

from app.config import Settings


def test_fetch_defaults_are_valid() -> None:
    settings = Settings(_env_file=None)
    assert settings.fetch_heartbeat_stall_minutes == 5
    assert settings.fetch_heartbeat_interval_seconds == 30
    assert settings.fetch_watchdog_interval_seconds == 60
    assert settings.fetch_worker_concurrency == 2
    assert settings.fetch_agent_max_rounds == 2


def test_agent_max_rounds_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, fetch_agent_max_rounds=0)
    assert Settings(_env_file=None, fetch_agent_max_rounds=1).fetch_agent_max_rounds == 1


def test_interval_must_be_below_the_stall_threshold() -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            fetch_heartbeat_stall_minutes=5,
            fetch_heartbeat_interval_seconds=300,
        )


@pytest.mark.parametrize(
    "key",
    [
        "fetch_heartbeat_stall_minutes",
        "fetch_heartbeat_interval_seconds",
        "fetch_watchdog_interval_seconds",
        "fetch_worker_concurrency",
    ],
)
def test_every_fetch_setting_must_be_positive(key) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{key: 0})
