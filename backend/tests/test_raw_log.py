import json
import re

import httpx

from app.llm.chat import ChatClient
from app.llm.raw_log import InMemoryRawLogStore, RawCallLogger
from tests.llm_helpers import FakeSleep, FakeTransport, chat_response, make_settings

KEY_PATTERN = re.compile(r"^llm-calls/\d{4}/\d{2}/\d{2}/[0-9a-f]{32}\.json$")


def test_one_object_per_attempt_and_secrets_masked() -> None:
    store = InMemoryRawLogStore()
    logger = RawCallLogger(store, secrets=("evren-key",))
    fake = FakeTransport(evren=[httpx.Response(429), chat_response()])
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(fake),
        sleep=FakeSleep(),
        raw_logger=logger,
    )

    client.complete([{"role": "user", "content": "hi"}])

    assert len(store.objects) == 2
    for key in store.objects:
        assert KEY_PATTERN.match(key), key

    payloads = [json.loads(raw.decode("utf-8")) for raw in store.objects.values()]
    attempts = sorted(payload["attempt"] for payload in payloads)
    assert attempts == [1, 2]
    providers = {payload["provider"] for payload in payloads}
    assert providers == {"evren"}

    for raw in store.objects.values():
        text = raw.decode("utf-8")
        assert "evren-key" not in text
        assert "Bearer" not in text
        payload = json.loads(text)
        assert payload["request"]["headers"]["Authorization"] == "***"


def test_usage_and_cost_are_recorded() -> None:
    store = InMemoryRawLogStore()
    logger = RawCallLogger(store, secrets=("evren-key",))
    usage = {"cost": 0.01, "evren": {"credits_remaining_cr": 5, "routed_model": "x"}}
    fake = FakeTransport(evren=[chat_response(usage=usage)])
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(fake),
        sleep=FakeSleep(),
        raw_logger=logger,
    )

    client.complete([{"role": "user", "content": "hi"}])

    payload = json.loads(next(iter(store.objects.values())).decode("utf-8"))
    assert payload["cost"] == 0.01
    assert payload["usage"]["evren"]["credits_remaining_cr"] == 5


class _RaisingStore:
    def put(self, key: str, payload: bytes) -> None:
        raise RuntimeError("storage down")


def test_logging_failure_does_not_break_the_call() -> None:
    logger = RawCallLogger(_RaisingStore(), secrets=("evren-key",))
    fake = FakeTransport(evren=[chat_response()])
    client = ChatClient(
        make_settings(),
        transport=httpx.MockTransport(fake),
        sleep=FakeSleep(),
        raw_logger=logger,
    )

    result = client.complete([{"role": "user", "content": "hi"}])

    assert result.content == "ok"
