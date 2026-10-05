"""Resolve the frequency of a dataset whose source hides the ``FREQ`` dimension.

Some databrowser2 dataflows declare ``FREQ`` in ``template.hiddenDimensions`` and
never expose it as a data dimension, so the dataset carries no frequency for
:func:`app.connectors.base.build_series_definition`. The source still tells the
frequency through the hidden dimension's partial codelist; this module turns that
codelist into one of five explicit resolutions and the attribute keys the catalog
stores.

The functions here are pure except :func:`resolve_for_dataset`, which only makes
the one partial-codelist call (codes and ``obsCount``, never observation values).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.connectors.base import ConnectorError
from app.data.errors import PeriodError
from app.data.periods import frequency_for_sdmx_code

#: Resolution statuses (frozen; the catalog stores exactly these strings).
SINGLE = "single"
MULTIPLE = "multiple"
EMPTY = "empty"
UNKNOWN_CODE = "unknown_code"
QUERY_ERROR = "query_error"

STATUSES = (SINGLE, MULTIPLE, EMPTY, UNKNOWN_CODE, QUERY_ERROR)

#: Statuses that must DROP a previously stored single frequency (a stale value
#: would be wrong now).
DROP_STATUSES = (MULTIPLE, EMPTY, UNKNOWN_CODE)


class _CodelistClient(Protocol):
    def dataset_partial_codelist(
        self, dataset_id: str, dimension: str, criteria: list[dict[str, Any]] | None = None
    ) -> Any:
        """Return a response whose ``json()`` is the partial codelist payload."""


@dataclass(frozen=True)
class FrequencyResolution:
    """The outcome of reading the hidden ``FREQ`` codelist."""

    status: str
    codes: list[dict[str, str]] = field(default_factory=list)
    code: str | None = None
    frequency: str | None = None
    reason: str | None = None


def _freq_criterion(payload: Any) -> tuple[dict[str, Any] | None, str]:
    """Return ``(criterion, reason)``; ``None`` with a reason when absent."""
    if not isinstance(payload, dict):
        return None, "payload is not an object"
    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return None, "criterion missing"
    for criterion in criteria:
        if isinstance(criterion, dict) and criterion.get("id") == "FREQ":
            return criterion, ""
    return None, "criterion missing"


def resolve_frequency(payload_or_error: Any) -> FrequencyResolution:
    """Resolve a partial-codelist payload (or a client error) into a status.

    A :class:`ConnectorError` (the query failed) becomes ``query_error``; any
    other exception is a programming error and is re-raised, never hidden as a
    query failure. A payload without usable selectable codes never becomes
    ``query_error`` either.
    """
    if isinstance(payload_or_error, BaseException):
        if isinstance(payload_or_error, ConnectorError):
            return FrequencyResolution(status=QUERY_ERROR, reason=str(payload_or_error))
        raise payload_or_error

    criterion, reason = _freq_criterion(payload_or_error)
    if criterion is None:
        return FrequencyResolution(status=EMPTY, reason=reason)
    values = criterion.get("values")
    if not isinstance(values, list) or not values:
        return FrequencyResolution(status=EMPTY, reason="values empty")
    valid = [
        {
            "id": str(value["id"]),
            "name": str(value.get("name") or value.get("label") or value["id"]),
        }
        for value in values
        if isinstance(value, dict) and value.get("isSelectable") and value.get("id") is not None
    ]
    if not valid:
        return FrequencyResolution(status=EMPTY, reason="no selectable codes")
    if len(valid) > 1:
        return FrequencyResolution(
            status=MULTIPLE, codes=valid, reason=f"{len(valid)} selectable codes"
        )
    code = valid[0]["id"]
    try:
        frequency = frequency_for_sdmx_code(code)
    except PeriodError:
        return FrequencyResolution(
            status=UNKNOWN_CODE,
            codes=valid,
            code=code,
            reason=f"unknown FREQ code {code!r}",
        )
    return FrequencyResolution(status=SINGLE, codes=valid, code=code, frequency=frequency)


def as_attributes(resolution: FrequencyResolution, *, checked_at: str) -> dict[str, Any]:
    """The ``attributes`` keys to store for ``resolution``.

    ``frequency_resolution`` is always written; ``default_frequency`` and
    ``frequency_source`` only for a single resolved code (our frequency
    vocabulary, never a reduced multiple).
    """
    attributes: dict[str, Any] = {
        "frequency_resolution": {
            "status": resolution.status,
            "codes": [dict(code) for code in resolution.codes],
            "reason": resolution.reason,
            "checked_at": checked_at,
        }
    }
    if resolution.status == SINGLE:
        attributes["default_frequency"] = resolution.frequency
        name = resolution.codes[0]["name"] if resolution.codes else None
        attributes["frequency_source"] = {
            "code": resolution.code,
            "name": name,
            "origin": "hidden_freq_codelist",
            "checked_at": checked_at,
        }
    return attributes


def resolve_for_dataset(client: _CodelistClient, identifier: str) -> FrequencyResolution:
    """Query the dataset's hidden ``FREQ`` partial codelist and resolve it.

    The identifier is the source's ``TR,<dataflow_id>,<version>`` form. A client
    failure (``ConnectorError``, including an unparseable payload) becomes
    ``query_error``; anything else propagates.
    """
    try:
        response = client.dataset_partial_codelist(identifier, "FREQ")
        payload = response.json()
    except ConnectorError as exc:
        return resolve_frequency(exc)
    return resolve_frequency(payload)


__all__ = [
    "DROP_STATUSES",
    "EMPTY",
    "MULTIPLE",
    "QUERY_ERROR",
    "SINGLE",
    "STATUSES",
    "UNKNOWN_CODE",
    "FrequencyResolution",
    "as_attributes",
    "resolve_for_dataset",
    "resolve_frequency",
]
