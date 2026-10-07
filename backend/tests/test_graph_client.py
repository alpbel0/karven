from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.graph.client import create_driver
from app.graph.errors import GraphError


def _settings(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "neo4j_enabled": True,
        "neo4j_uri": "bolt://127.0.0.1:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": SecretStr("pw"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_disabled_graph_raises_clear_error() -> None:
    with pytest.raises(GraphError, match="disabled"):
        create_driver(_settings(neo4j_enabled=False))


def test_incomplete_settings_raise_clear_error() -> None:
    with pytest.raises(GraphError, match="incomplete"):
        create_driver(_settings(neo4j_uri=None))
    with pytest.raises(GraphError, match="NEO4J_PASSWORD"):
        create_driver(_settings(neo4j_password=None))
    with pytest.raises(GraphError, match="NEO4J_USER"):
        create_driver(_settings(neo4j_user=None))
