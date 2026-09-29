"""Synchronous OpenAI-compatible chat client with EVREN → OpenRouter fallback."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.llm.errors import LLMOutputError, LLMRequestError
from app.llm.policy import ProviderRuntime, RetrySpec, request_with_fallback
from app.llm.raw_log import RawCallLogger
from app.prompts.ref import PromptRef

CHAT_RETRY_SPEC = RetrySpec(
    fatal_statuses=frozenset({400, 401, 404, 422}),
    # Credit exhausted on EVREN: switch to OpenRouter instead of failing.
    fallback_statuses=frozenset({402}),
)


@dataclass
class ChatResult:
    """Normalized result of a chat completion."""

    content: str | None
    tool_calls: list[dict[str, Any]] | None
    finish_reason: str | None
    usage: dict[str, Any] | None
    provider: str
    model: str
    raw: dict[str, Any]


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMOutputError("provider returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise LLMOutputError("provider returned a non-object response")
    return data


@dataclass(frozen=True)
class _ProviderConfig:
    name: str
    model: str
    base_url: str
    api_key: str


class ChatClient:
    """Chat client sending OpenAI-compatible requests, EVREN first."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
        raw_logger: RawCallLogger | None = None,
    ) -> None:
        self._settings = settings
        self._sleep = sleep or time.sleep
        self._raw_logger = raw_logger
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.Client(
                transport=transport,
                timeout=settings.llm_request_timeout_s,
            )

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        prompt_ref: PromptRef | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "messages": list(messages),
            "max_tokens": (max_tokens if max_tokens is not None else self._settings.llm_max_tokens),
        }
        if tools is not None:
            body["tools"] = list(tools)
        if response_format is not None:
            body["response_format"] = response_format

        providers = self._build_providers(body, prompt_ref)
        provider, response = request_with_fallback(
            providers,
            spec=CHAT_RETRY_SPEC,
            retry_waits=self._settings.llm_retry_waits_s,
            max_total_wait_s=self._settings.llm_max_total_wait_s,
            transient_attempts=self._settings.llm_transient_attempts,
            sleep=self._sleep,
        )
        data = _safe_json(response)
        return self._to_result(provider, data)

    def _build_providers(
        self, body: dict[str, Any], prompt_ref: PromptRef | None = None
    ) -> list[ProviderRuntime]:
        configs = self._provider_configs()
        providers: list[ProviderRuntime] = []
        for config in configs:
            base = config.base_url.rstrip("/")
            url = f"{base}/chat/completions"
            request_body = {"model": config.model, **body}
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

            def send(
                attempt: int,
                *,
                config: _ProviderConfig = config,
                url: str = url,
                request_body: dict[str, Any] = request_body,
                headers: dict[str, str] = headers,
                prompt_ref: PromptRef | None = prompt_ref,
            ) -> httpx.Response:
                start = time.perf_counter()
                try:
                    response = self._http.post(url, json=request_body, headers=headers)
                except httpx.HTTPError as exc:
                    self._log(
                        config=config,
                        url=url,
                        request_body=request_body,
                        headers=headers,
                        attempt=attempt,
                        duration_ms=(time.perf_counter() - start) * 1000,
                        error=repr(exc),
                        prompt_ref=prompt_ref,
                    )
                    raise
                self._log(
                    config=config,
                    url=url,
                    request_body=request_body,
                    headers=headers,
                    attempt=attempt,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    status_code=response.status_code,
                    response_body=_response_body(response),
                    prompt_ref=prompt_ref,
                )
                return response

            providers.append(ProviderRuntime(name=config.name, model=config.model, send=send))
        return providers

    def _provider_configs(self) -> list[_ProviderConfig]:
        configs: list[_ProviderConfig] = []
        if self._settings.evren_api_key is not None:
            configs.append(
                _ProviderConfig(
                    name="evren",
                    model=self._settings.evren_model,
                    base_url=self._settings.evren_base_url,
                    api_key=self._settings.evren_api_key.get_secret_value(),
                )
            )
        if self._settings.openrouter_api_key is not None:
            configs.append(
                _ProviderConfig(
                    name="openrouter",
                    model=self._settings.openrouter_model,
                    base_url=self._settings.openrouter_base_url,
                    api_key=self._settings.openrouter_api_key.get_secret_value(),
                )
            )
        if not configs:
            raise LLMRequestError("no LLM provider credentials configured")
        return configs

    def _log(
        self,
        *,
        config: _ProviderConfig,
        url: str,
        request_body: dict[str, Any],
        headers: dict[str, str],
        attempt: int,
        duration_ms: float,
        status_code: int | None = None,
        response_body: Any = None,
        error: str | None = None,
        prompt_ref: PromptRef | None = None,
    ) -> None:
        if self._raw_logger is None:
            return
        self._raw_logger.record(
            provider=config.name,
            model=config.model,
            url=url,
            request_body=request_body,
            request_headers=headers,
            attempt=attempt,
            duration_ms=duration_ms,
            status_code=status_code,
            response_body=response_body,
            error=error,
            prompt=prompt_ref,
        )

    @staticmethod
    def _to_result(provider: ProviderRuntime, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices")
        if not choices:
            raise LLMOutputError("provider returned no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        return ChatResult(
            content=message.get("content"),
            tool_calls=message.get("tool_calls") or None,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            provider=provider.name,
            model=provider.model,
            raw=data,
        )


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
