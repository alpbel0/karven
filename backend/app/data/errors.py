"""Errors raised by the data layer."""


class DataError(Exception):
    """Base class for every data-layer error."""


class PeriodError(DataError, ValueError):
    """A period date is not aligned with the series frequency."""


class SeriesNotFoundError(DataError):
    """No series matches the requested id."""


class SeriesDefinitionError(DataError, ValueError):
    """The requested dimension codes do not form a valid series."""
