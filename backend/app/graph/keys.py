"""Pure key builders for the relation graph (no I/O).

``Series.key`` is ``"<institution>|<code>"``; the separator must not appear in
either part, otherwise the key would be ambiguous. ``Relation.key`` is a
deterministic hash of the target key and the *set* of driver keys, so driver
order does not matter, duplicate drivers collapse, and the reverse direction
(which swaps target and drivers) is a different key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from app.graph.errors import InvalidRelationError

SERIES_KEY_SEPARATOR = "|"


def series_key(institution: str, code: str) -> str:
    """Build a ``Series.key``; reject empty parts or a part containing ``|``."""
    if not isinstance(institution, str) or not isinstance(code, str):
        raise InvalidRelationError("institution and code must be strings")
    if not institution or not code:
        raise InvalidRelationError("institution and code must be non-empty")
    if SERIES_KEY_SEPARATOR in institution or SERIES_KEY_SEPARATOR in code:
        raise InvalidRelationError(
            f"institution and code must not contain the {SERIES_KEY_SEPARATOR!r} separator"
        )
    return f"{institution}{SERIES_KEY_SEPARATOR}{code}"


def relation_key(target_key: str, driver_keys: Iterable[str]) -> str:
    """Build a deterministic ``Relation.key`` from the target and driver keys.

    The driver keys are treated as a set: order is irrelevant and duplicates
    collapse. The canonical JSON form is hashed so the key stays short and the
    parts can never be confused with each other.
    """
    if not isinstance(target_key, str) or not target_key:
        raise InvalidRelationError("target_key must be a non-empty string")
    keys = list(driver_keys)
    for key in keys:
        if not isinstance(key, str) or not key:
            raise InvalidRelationError("driver keys must be non-empty strings")
    drivers = sorted(set(keys))
    canonical = json.dumps(
        {"target": target_key, "drivers": drivers},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
