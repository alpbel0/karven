"""Unit tests for the per-tool call limit in ``run_tool_loop`` (no network)."""

from __future__ import annotations

import json

import httpx

from app.llm.chat import ChatClient
from app.llm.tool_loop import run_tool_loop
from tests.llm_helpers import FakeSleep, FakeTransport, chat_response, make_settings, tool_call

FINAL_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


def _client(fake: FakeTransport) -> ChatClient:
    return ChatClient(make_settings(), transport=httpx.MockTransport(fake), sleep=FakeSleep())


def _tool_messages(result: object) -> list[dict]:
    return [m for m in result.messages if m.get("role") == "tool"]  # type: ignore[attr-defined]


def test_tool_limit_refuses_the_fifth_call_with_exact_message() -> None:
    executed: list[int] = []

    def record(a: int) -> dict:
        executed.append(a)
        return {"echo": a}

    tools = {
        "find": (
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            record,
        )
    }
    calls = [tool_call(f"call_{i}", "find", {"a": i}) for i in range(6)]
    fake = FakeTransport(
        evren=[chat_response(content=None, tool_calls=calls), chat_response(content='{"ok": true}')]
    )
    client = _client(fake)

    result = run_tool_loop(
        client,
        [{"role": "user", "content": "go"}],
        tools,
        FINAL_SCHEMA,
        max_tool_calls=6,
        tool_limits={"find": 4},
    )

    assert executed == [0, 1, 2, 3]
    assert result.tool_calls == 6
    messages = _tool_messages(result)
    assert len(messages) == 6
    assert [json.loads(m["content"]) for m in messages[:4]] == [
        {"echo": 0},
        {"echo": 1},
        {"echo": 2},
        {"echo": 3},
    ]
    for message in messages[4:]:
        assert json.loads(message["content"]) == {"error": "tool call limit reached for find (4)"}


def test_budget_refund_output_does_not_count_toward_tool_limit() -> None:
    executed: list[int] = []

    def refund(a: int) -> dict:
        executed.append(a)
        return {"budget_refund": True, "echo": a}

    tools = {
        "refunder": (
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            refund,
        )
    }
    calls = [tool_call(f"call_{i}", "refunder", {"a": i}) for i in range(4)]
    fake = FakeTransport(
        evren=[chat_response(content=None, tool_calls=calls), chat_response(content='{"ok": true}')]
    )
    client = _client(fake)

    result = run_tool_loop(
        client,
        [{"role": "user", "content": "go"}],
        tools,
        FINAL_SCHEMA,
        max_tool_calls=4,
        tool_limits={"refunder": 1},
    )

    assert executed == [0, 1, 2, 3]
    assert result.tool_calls == 4
    assert all(
        json.loads(message["content"]).get("echo") is not None for message in _tool_messages(result)
    )


def test_global_max_tool_calls_still_terminates() -> None:
    executed: list[int] = []

    def record(a: int) -> int:
        executed.append(a)
        return a

    tools = {
        "find": (
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            record,
        )
    }
    calls = [tool_call(f"call_{i}", "find", {"a": i}) for i in range(5)]
    fake = FakeTransport(
        evren=[chat_response(content=None, tool_calls=calls), chat_response(content='{"ok": true}')]
    )
    client = _client(fake)

    result = run_tool_loop(
        client,
        [{"role": "user", "content": "go"}],
        tools,
        FINAL_SCHEMA,
        max_tool_calls=2,
        tool_limits={"find": 99},
    )

    assert executed == [0, 1]
    assert result.tool_calls == 2
    assert len(_tool_messages(result)) == 2


def test_tool_limits_none_keeps_original_behaviour() -> None:
    executed: list[int] = []

    def record(a: int) -> int:
        executed.append(a)
        return a

    tools = {
        "find": (
            {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
            record,
        )
    }
    calls = [tool_call(f"call_{i}", "find", {"a": i}) for i in range(3)]
    fake = FakeTransport(
        evren=[chat_response(content=None, tool_calls=calls), chat_response(content='{"ok": true}')]
    )
    client = _client(fake)

    result = run_tool_loop(
        client,
        [{"role": "user", "content": "go"}],
        tools,
        FINAL_SCHEMA,
        max_tool_calls=3,
    )

    assert executed == [0, 1, 2]
    assert result.tool_calls == 3
