"""Relation graph layer (Neo4j) for Task 3.1.

Public surface: the frozen records, the error types, the pure key builders, the
driver/session helpers and the repository read/write functions.
"""

from app.graph import repository
from app.graph.client import create_driver, graph_client
from app.graph.errors import GraphError, InvalidRelationError, RelationNotFoundError
from app.graph.keys import relation_key, series_key
from app.graph.models import (
    EXPECTED_DIRECTIONS,
    PERIOD_RESULTS,
    PERIODS,
    RELATION_STATUSES,
    TRANSFORMS,
    Hypothesis,
    NewsLink,
    PeriodResult,
    RelationRecord,
    SeriesRef,
    TestRecord,
    normalize_relation,
    validate_driver_directions,
    validate_nominal_members,
    validate_status,
)

__all__ = [
    "EXPECTED_DIRECTIONS",
    "PERIODS",
    "PERIOD_RESULTS",
    "RELATION_STATUSES",
    "TRANSFORMS",
    "GraphError",
    "Hypothesis",
    "InvalidRelationError",
    "NewsLink",
    "PeriodResult",
    "RelationNotFoundError",
    "RelationRecord",
    "SeriesRef",
    "TestRecord",
    "create_driver",
    "graph_client",
    "normalize_relation",
    "relation_key",
    "repository",
    "series_key",
    "validate_driver_directions",
    "validate_nominal_members",
    "validate_status",
]
