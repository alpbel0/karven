"""Shared fakes for the LLM client tests (no network, no real sleeping)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "evren_api_key": "evren-key",
        "evren_base_url": "https://evren.test/v1",
        "openrouter_api_key": "openrouter-key",
        "openrouter_base_url": "https://openrouter.test/api/v1",
        "typesafe_api_key": "typesafe-key",
        "typesafe_base_url": "https://typesafe.test/v1",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakeSleep:
    """Records requested wait durations instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class FakeTransport:
    """httpx transport double routing queued responses by provider host.

    Each queued item is either an ``httpx.Response`` or an exception instance
    to raise (for timeout/connection-error tests).
    """

    def __init__(
        self,
        evren: list[Any] | None = None,
        openrouter: list[Any] | None = None,
        typesafe: list[Any] | None = None,
    ) -> None:
        self.evren: list[Any] = list(evren or [])
        self.openrouter: list[Any] = list(openrouter or [])
        self.typesafe: list[Any] = list(typesafe or [])
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        host = request.url.host
        if "typesafe" in host:
            provider, queue = "typesafe", self.typesafe
        elif "evren" in host:
            provider, queue = "evren", self.evren
        elif "openrouter" in host:
            provider, queue = "openrouter", self.openrouter
        else:
            raise AssertionError(f"unexpected host in test: {host}")
        self.calls.append((provider, body, str(request.url)))
        if not queue:
            raise AssertionError(f"no queued response for {provider}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def providers_called(self) -> list[str]:
        return [provider for provider, _body, _url in self.calls]


def chat_response(
    content: str | None = "ok",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": usage if usage is not None else {},
        },
    )


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
