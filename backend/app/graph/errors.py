"""Errors for the relation graph layer."""


class GraphError(Exception):
    """Base class for every graph-layer failure."""


class InvalidRelationError(GraphError):
    """Raised when a relation, hypothesis or test input is invalid."""


class RelationNotFoundError(GraphError):
    """Raised when a relation key is not present in the graph."""
