import json

import httpx

from app.llm.chat import ChatClient
from app.llm.raw_log import InMemoryRawLogStore, RawCallLogger
from app.llm.tool_loop import run_tool_loop
from app.prompts.ref import PromptRef
from tests.llm_helpers import FakeSleep, FakeTransport, chat_response, make_settings, tool_call

FINAL_SCHEMA = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}},
    "required": ["sum"],
}


def _payloads(store: InMemoryRawLogStore) -> list[dict]:
    return [json.loads(raw.decode("utf-8")) for raw in store.objects.values()]


def test_complete_records_prompt_ref() -> None:
    store = InMemoryRawLogStore()
    logger = RawCallLogger(store, secrets=("evren-key",))
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(FakeTransport(evren=[chat_response()])),
        sleep=FakeSleep(),
        raw_logger=logger,
    )
    ref = PromptRef(key="first_agent.system", version=2, checksum="abc123")

    client.complete([{"role": "user", "content": "hi"}], prompt_ref=ref)

    payload = _payloads(store)[0]
    assert payload["prompt"] == {
        "key": "first_agent.system",
        "version": 2,
        "checksum": "abc123",
    }


def test_complete_without_prompt_logs_null() -> None:
    store = InMemoryRawLogStore()
    logger = RawCallLogger(store, secrets=("evren-key",))
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(FakeTransport(evren=[chat_response()])),
        sleep=FakeSleep(),
        raw_logger=logger,
    )

    client.complete([{"role": "user", "content": "hi"}])

    assert _payloads(store)[0]["prompt"] is None


def test_every_retry_attempt_carries_prompt_ref() -> None:
    store = InMemoryRawLogStore()
    logger = RawCallLogger(store, secrets=("evren-key",))
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(FakeTransport(evren=[httpx.Response(429), chat_response()])),
        sleep=FakeSleep(),
        raw_logger=logger,
    )
    ref = PromptRef(key="graph_agent.system", version=1, checksum="c0ffee")

    client.complete([{"role": "user", "content": "hi"}], prompt_ref=ref)

    payloads = _payloads(store)
    assert len(payloads) == 2
    assert all(payload["prompt"]["checksum"] == "c0ffee" for payload in payloads)


def test_tool_loop_passes_prompt_ref_to_every_call() -> None:
    store = InMemoryRawLogStore()
    logger = RawCallLogger(store, secrets=("evren-key",))
    fake = FakeTransport(
        evren=[
            chat_response(content=None, tool_calls=[tool_call("c1", "add", {"a": 1, "b": 2})]),
            chat_response(content="thinking"),
            chat_response(content='{"sum": 3}'),
        ]
    )
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(fake),
        sleep=FakeSleep(),
        raw_logger=logger,
    )
    ref = PromptRef(key="first_agent.system", version=4, checksum="feed")

    run_tool_loop(
        client,
        [{"role": "user", "content": "add"}],
        {"add": ({"type": "object", "properties": {}}, lambda **_: 3)},
        FINAL_SCHEMA,
        max_tool_calls=5,
        prompt_ref=ref,
    )

    payloads = _payloads(store)
    assert len(payloads) == 3
    assert all(payload["prompt"]["version"] == 4 for payload in payloads)
