"""Retry/fallback policy shared by the chat and Jev clients.

The policy is transport-agnostic: callers provide a ``send`` callable per
provider that performs one HTTP attempt (and records the raw log). The policy
classifies the outcome, retries transient failures and 429s, and falls back to
the next provider when the current one is exhausted.
"""

from __future__ import annotations

import email.utils
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.llm.errors import (
    LLMAllProvidersFailed,
    LLMError,
    LLMProviderUnavailable,
    LLMRateLimitError,
    LLMRequestError,
)

logger = logging.getLogger(__name__)

MAX_SINGLE_WAIT_S = 120.0


@dataclass
class ProviderRuntime:
    """A single provider attempt target."""

    name: str
    model: str
    send: Callable[[int], httpx.Response]


@dataclass(frozen=True)
class RetrySpec:
    """Provider-agnostic classification of HTTP status codes."""

    fatal_statuses: frozenset[int]
    fallback_statuses: frozenset[int] = field(default_factory=frozenset)
    rate_limit_statuses: frozenset[int] = field(default_factory=lambda: frozenset({429}))


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = (parsed - datetime.now(UTC)).total_seconds()
    return max(delta, 0.0)


def _next_wait(waits: Sequence[int], index: int) -> float:
    if not waits:
        return 0.0
    if index < len(waits):
        return float(waits[index])
    return float(waits[-1])


def _safe_body(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text


def _build_error(
    provider: str,
    status: int,
    body: object,
    *,
    retry_after: float | None = None,
) -> LLMError:
    if status == 429:
        return LLMRateLimitError(
            f"{provider} rate limited (429)",
            provider=provider,
            status_code=status,
            body=body,
            retry_after=retry_after,
        )
    if status == 402:
        return LLMProviderUnavailable(
            f"{provider} credit exhausted (402)",
            provider=provider,
            status_code=status,
            body=body,
        )
    if status >= 500:
        return LLMProviderUnavailable(
            f"{provider} unavailable ({status})",
            provider=provider,
            status_code=status,
            body=body,
        )
    return LLMRequestError(
        f"{provider} request failed ({status})",
        provider=provider,
        status_code=status,
        body=body,
    )


def request_with_fallback(
    providers: Sequence[ProviderRuntime],
    *,
    spec: RetrySpec,
    retry_waits: Sequence[int],
    max_total_wait_s: float,
    transient_attempts: int,
    sleep: Callable[[float], None],
) -> tuple[ProviderRuntime, httpx.Response]:
    """Run one logical request across providers with retry and fallback.

    Returns the successful ``(provider, response)`` pair. Raises the provider
    error immediately for fatal statuses, or :class:`LLMAllProvidersFailed`
    once every provider is exhausted.
    """
    errors: list[tuple[str, LLMError]] = []
    attempt_number = 0

    for provider in providers:
        transient_failures = 0
        cumulative_wait = 0.0
        wait_index = 0
        pending_error: LLMError | None = None

        while True:
            attempt_number += 1
            try:
                response = provider.send(attempt_number)
            except httpx.TransportError:
                transient_failures += 1
                pending_error = LLMProviderUnavailable(
                    f"{provider.name} transport error", provider=provider.name
                )
                if transient_failures >= transient_attempts:
                    break
                continue

            status = response.status_code
            body = _safe_body(response)

            if 200 <= status < 300:
                return provider, response

            if status in spec.rate_limit_statuses:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                wait = (
                    retry_after if retry_after is not None else _next_wait(retry_waits, wait_index)
                )
                wait = min(wait, MAX_SINGLE_WAIT_S)
                wait_index += 1
                pending_error = _build_error(provider.name, status, body, retry_after=retry_after)
                # Fixed, greppable text: Task 0.5 needs a live occurrence to close.
                logger.warning(
                    "llm rate limited 429: provider=%s retry_after=%s wait=%.0fs waited=%.0fs",
                    provider.name,
                    retry_after,
                    wait,
                    cumulative_wait,
                )
                if cumulative_wait + wait > max_total_wait_s:
                    break
                sleep(wait)
                cumulative_wait += wait
                continue

            if status in spec.fallback_statuses:
                pending_error = _build_error(provider.name, status, body)
                break

            if status in spec.fatal_statuses:
                raise _build_error(provider.name, status, body)

            if status >= 500:
                transient_failures += 1
                pending_error = _build_error(provider.name, status, body)
                if transient_failures >= transient_attempts:
                    break
                continue

            # Any other status is unexpected and not worth retrying.
            raise _build_error(provider.name, status, body)

        if pending_error is None:
            pending_error = LLMProviderUnavailable(
                f"{provider.name} exhausted", provider=provider.name
            )
        errors.append((provider.name, pending_error))

    raise LLMAllProvidersFailed("all LLM providers failed", errors=errors)
