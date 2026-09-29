"""Lightweight reference to an exact prompt version (no database imports)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptRef:
    """Identifies one immutable prompt version for logging and tracing."""

    key: str
    version: int
    checksum: str
