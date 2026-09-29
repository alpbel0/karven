import os

import pytest

from app.integration_safety import validate_integration_env


@pytest.fixture(scope="session", autouse=True)
def _require_test_environment() -> None:
    """Refuse to run unless the environment is the isolated test stack."""
    problems = validate_integration_env(os.environ)
    if problems:
        pytest.exit(
            "Refusing to run integration tests against non-test settings:\n  - "
            + "\n  - ".join(problems)
            + "\nStart the isolated stack with: uv run python scripts/integration.py",
            returncode=1,
        )
