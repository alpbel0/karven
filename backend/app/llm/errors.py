"""Exception hierarchy for the LLM client package."""

from __future__ import annotations

from typing import Any


class LLMError(Exception):
    """Base class for every error raised by the LLM client package."""


class LLMHTTPError(LLMError):
    """An error tied to a specific provider HTTP response.

    Attributes:
        provider: provider name (``"evren"``, ``"openrouter"``, ``"typesafe"``).
        status_code: HTTP status code returned by the provider, if any.
        body: decoded response body, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.body = body


class LLMRequestError(LLMHTTPError):
    """The request or credentials are wrong (e.g. 400/401/404/422).

    These errors are never retried and never trigger a provider fallback.
    """


class LLMRateLimitError(LLMHTTPError):
    """HTTP 429: the provider asks the client to slow down."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        body: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status_code=status_code, body=body)
        self.retry_after = retry_after


class LLMProviderUnavailable(LLMHTTPError):
    """Transient provider failure: timeout, connection error, 5xx, overload."""


class LLMOutputError(LLMError):
    """The provider answered, but the output could not be used as requested."""


class LLMAllProvidersFailed(LLMError):
    """Every configured provider failed for a single logical call.

    Attributes:
        errors: list of ``(provider_name, exception)`` pairs in attempt order.
    """

    def __init__(self, message: str, *, errors: list[tuple[str, LLMError]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[tuple[str, LLMError]] = list(errors or [])
