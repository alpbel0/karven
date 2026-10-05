"""Full article text extraction with trafilatura (Task 1.7).

A single extraction point so a per-site selector fallback can be added later
without touching the service. ``trafilatura`` is used with ``favor_recall=True``
and comments excluded (measured 2026-10-03 on the real fixtures: Sabah 1065
chars, Habertürk 1638, Sözcü 1616, BloombergHT 1823, CNN Türk 1518).

Sabah's extracted text begins with ``Giriş Tarihi:`` / ``Son Güncelleme:``
header lines; :func:`extract_with_error` strips those leading lines generically
for every source. An empty result, or one that only held such header lines,
returns no text.

Two entry points:

- :func:`extract_text` returns only the text (``str | None``) for callers that
  do not need the reason.
- :func:`extract_with_error` returns an :class:`ExtractResult` whose ``error``
  carries the exception summary when trafilatura itself failed, so the failure
  is not lost (the row records ``text_error = "extraction error: ..."``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import trafilatura

logger = logging.getLogger(__name__)

#: Leading article-header line prefixes to drop (Sabah emits these).
_LEADING_PREFIXES = ("Giriş Tarihi:", "Son Güncelleme:")


@dataclass(frozen=True)
class ExtractResult:
    """The extracted text plus why it is missing (``None`` when it is not)."""

    text: str | None
    error: str | None = None


def _clean(text: str) -> str | None:
    """Drop leading header/blank lines and trim; ``None`` when nothing is left."""
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "" or stripped.startswith(_LEADING_PREFIXES):
            start = index + 1
            continue
        break
    cleaned = "\n".join(lines[start:]).strip()
    return cleaned or None


def extract_with_error(html: str | bytes) -> ExtractResult:
    """Extract the article text; keep the trafilatura failure reason.

    ``error`` is set only when trafilatura raised (the exception is logged too);
    an empty or header-only result has ``error=None`` and ``text=None``.
    """
    if not html:
        return ExtractResult(None)
    try:
        raw = trafilatura.extract(html, favor_recall=True, include_comments=False)
    except Exception as exc:  # noqa: BLE001 - recorded as a no_text reason
        logger.warning("news text extraction failed", exc_info=True)
        return ExtractResult(None, f"{type(exc).__name__}: {exc}")
    if not raw:
        return ExtractResult(None)
    return ExtractResult(_clean(raw))


def extract_text(html: str | bytes) -> str | None:
    """Extract the article body text, or ``None`` when extraction yields nothing."""
    return extract_with_error(html).text


__all__ = ["ExtractResult", "extract_text", "extract_with_error"]
