"""Errors of the relation test engine."""

from __future__ import annotations


class AnalysisError(Exception):
    """Base class of every engine error."""


class UnsupportedSeriesError(AnalysisError):
    """A series cannot enter a relation test (measure type, frequency, non-positive levels...)."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


class InvalidSpecError(AnalysisError, ValueError):
    """The relation specification itself is invalid."""


class SingularDesignError(AnalysisError):
    """The regression design is singular (for example two identical drivers): a structural
    problem of the data, reported by the engine as ``insufficient_data``."""


class NumericalFailure(AnalysisError):
    """A computation returned a non-finite statistic. Never hidden: the calibration study counts
    it as ``failed`` and the protocol investigates a cell where it exceeds 0.1 %."""
