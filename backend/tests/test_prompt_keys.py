import pytest

from app.prompts.errors import PromptKeyError
from app.prompts.service import validate_key

VALID_KEYS = [
    "first_agent.system",
    "first_agent.final",
    "graph_agent.system",
    "jev.search_tag_score",
    "a.b",
    "a1.b2_c",
    "a.b.c.d",
    "1a.b",
]

INVALID_KEYS = [
    "",
    "a",
    "agent",
    "First.system",
    "agent.System",
    "a.",
    ".a.b",
    "a..b",
    "a.b.",
    "a.b!",
    "a b.c",
    "a-b.c",
    "a.b-c",
    "agent..system",
    "agent.system.",
]


@pytest.mark.parametrize("key", VALID_KEYS)
def test_valid_keys(key: str) -> None:
    assert validate_key(key) == key


@pytest.mark.parametrize("key", INVALID_KEYS)
def test_invalid_keys(key: str) -> None:
    with pytest.raises(PromptKeyError):
        validate_key(key)
