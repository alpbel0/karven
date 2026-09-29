import httpx

from app.llm.chat import ChatClient
from app.llm.tool_loop import run_tool_loop
from tests.llm_helpers import (
    FakeSleep,
    FakeTransport,
    chat_response,
    make_settings,
    tool_call,
)

FINAL_SCHEMA = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
}


def test_tools_only_on_tool_turns_and_response_format_only_on_final() -> None:
    executed: list[tuple[int, int]] = []

    def add(a: int, b: int) -> int:
        executed.append((a, b))
        return a + b

    tools = {
        "add": (
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
            add,
        )
    }

    fake = FakeTransport(
        evren=[
            chat_response(
                content=None,
                tool_calls=[tool_call("call_1", "add", {"a": 1, "b": 2})],
            ),
            chat_response(
                content=None,
                tool_calls=[tool_call("call_2", "add", {"a": 3, "b": 4})],
            ),
            chat_response(content="done"),
            chat_response(content='{"sum": 7}\n<|im_end|>'),
        ]
    )
    sleep = FakeSleep()
    client = ChatClient(make_settings(), transport=httpx.MockTransport(fake), sleep=sleep)

    result = run_tool_loop(
        client,
        [{"role": "user", "content": "add the numbers"}],
        tools,
        FINAL_SCHEMA,
        max_tool_calls=5,
    )

    assert result.output == {"sum": 7}
    assert result.tool_calls == 2
    assert executed == [(1, 2), (3, 4)]

    bodies = [body for _provider, body, _url in fake.calls]
    assert len(bodies) == 4
    for tool_body in bodies[:3]:
        assert "tools" in tool_body
        assert "response_format" not in tool_body
    final_body = bodies[3]
    assert "response_format" in final_body
    assert "tools" not in final_body
    assert final_body["response_format"]["json_schema"]["schema"] == FINAL_SCHEMA

    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]


def test_zero_max_tool_calls_sends_only_final_request() -> None:
    fake = FakeTransport(evren=[chat_response(content='{"sum": 0}')])
    sleep = FakeSleep()
    client = ChatClient(make_settings(), transport=httpx.MockTransport(fake), sleep=sleep)

    result = run_tool_loop(
        client,
        [{"role": "user", "content": "go"}],
        {},
        FINAL_SCHEMA,
        max_tool_calls=0,
    )

    assert result.output == {"sum": 0}
    assert len(fake.calls) == 1
    assert "tools" not in fake.calls[0][1]
    assert "response_format" in fake.calls[0][1]


def test_final_request_ends_with_user_instruction() -> None:
    # Live EVREN returned empty content when the final request ended with an
    # assistant message; the loop must close with a user instruction.
    fake = FakeTransport(
        evren=[chat_response(content="the answer is 5"), chat_response(content='{"sum": 5}')]
    )
    client = ChatClient(make_settings(), transport=httpx.MockTransport(fake), sleep=FakeSleep())

    run_tool_loop(
        client,
        [{"role": "user", "content": "add"}],
        {},
        FINAL_SCHEMA,
        max_tool_calls=3,
        final_instruction="Return JSON now.",
    )

    final_messages = fake.calls[-1][1]["messages"]
    assert final_messages[-1] == {"role": "user", "content": "Return JSON now."}
    assert final_messages[-2] == {"role": "assistant", "content": "the answer is 5"}
