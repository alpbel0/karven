"""Jev (TypeSafe) decision client with OpenRouter ``systemone`` fallback."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.llm.errors import LLMOutputError, LLMRequestError
from app.llm.policy import ProviderRuntime, RetrySpec, request_with_fallback
from app.llm.raw_log import RawCallLogger
from app.prompts.ref import PromptRef

JEV_RETRY_SPEC = RetrySpec(
    fatal_statuses=frozenset({400, 401, 404, 422}),
    fallback_statuses=frozenset({402}),
)


@dataclass(frozen=True)
class _JevProviderConfig:
    name: str
    model: str
    base_url: str
    api_key: str


class JevClient:
    """Decision client: TypeSafe first, OpenRouter ``systemone`` second."""

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

    def decide(
        self,
        state: Any,
        questions: dict[str, Any],
        *,
        prompt_ref: PromptRef | None = None,
    ) -> dict[str, Any]:
        """Ask Jev a set of questions and return answers plus usage/provider."""
        providers = self._build_providers(state, questions, prompt_ref)
        provider, response = request_with_fallback(
            providers,
            spec=JEV_RETRY_SPEC,
            retry_waits=self._settings.llm_retry_waits_s,
            max_total_wait_s=self._settings.llm_max_total_wait_s,
            transient_attempts=self._settings.llm_transient_attempts,
            sleep=self._sleep,
        )
        data = _safe_json(response)
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise LLMOutputError("Jev response did not contain answers")
        return {
            "answers": answers,
            "usage": data.get("usage"),
            "provider": provider.name,
            "model": provider.model,
        }

    def _build_providers(
        self, state: Any, questions: dict[str, Any], prompt_ref: PromptRef | None = None
    ) -> list[ProviderRuntime]:
        configs = self._provider_configs()
        providers: list[ProviderRuntime] = []
        for config in configs:
            base = config.base_url.rstrip("/")
            url = f"{base}/systemone"
            request_body = {
                "state": state,
                "model": config.model,
                "questions": questions,
            }
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

            def send(
                attempt: int,
                *,
                config: _JevProviderConfig = config,
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

    def _provider_configs(self) -> list[_JevProviderConfig]:
        configs: list[_JevProviderConfig] = []
        if self._settings.typesafe_api_key is not None:
            configs.append(
                _JevProviderConfig(
                    name="typesafe",
                    model=self._settings.jev_model,
                    base_url=self._settings.typesafe_base_url,
                    api_key=self._settings.typesafe_api_key.get_secret_value(),
                )
            )
        if self._settings.openrouter_api_key is not None:
            configs.append(
                _JevProviderConfig(
                    name="openrouter",
                    model=self._settings.jev_model,
                    base_url=self._settings.openrouter_base_url,
                    api_key=self._settings.openrouter_api_key.get_secret_value(),
                )
            )
        if not configs:
            raise LLMRequestError("no Jev provider credentials configured")
        return configs

    def _log(
        self,
        *,
        config: _JevProviderConfig,
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


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMOutputError("Jev provider returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise LLMOutputError("Jev provider returned a non-object response")
    return data


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
