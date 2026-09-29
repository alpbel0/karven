import httpx
import pytest

from app.llm.errors import LLMRequestError
from app.llm.jev import JevClient
from tests.llm_helpers import FakeSleep, FakeTransport, make_settings

QUESTIONS = {
    "risk": {
        "type": "score",
        "instructions": "How risky is this?",
        "criteria": ["very low", "low", "medium", "high", "very high"],
    }
}
ANSWERS = {
    "risk": {
        "type": "score",
        "score": 0.8,
        "probabilities": [0.05, 0.05, 0.1, 0.3, 0.5],
        "legend": ["very low", "low", "medium", "high", "very high"],
        "confidence": 0.7,
    }
}


def jev_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-latest",
            "answers": ANSWERS,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001},
        },
    )


def make_client(fake: FakeTransport, sleep: FakeSleep) -> JevClient:
    return JevClient(
        make_settings(),
        transport=httpx.MockTransport(fake),
        sleep=sleep,
    )


def test_typesafe_402_falls_back_to_openrouter_systemone() -> None:
    fake = FakeTransport(typesafe=[httpx.Response(402)], openrouter=[jev_response()])
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.decide("some state", QUESTIONS)

    assert result["provider"] == "openrouter"
    assert result["answers"] == ANSWERS
    assert result["usage"]["cost"] == 0.001
    assert fake.providers_called == ["typesafe", "openrouter"]
    assert fake.calls[0][2].endswith("/systemone")
    assert fake.calls[1][1]["model"] == "jev-latest"
    assert fake.calls[1][1]["questions"] == QUESTIONS


def test_typesafe_401_raises_without_fallback() -> None:
    fake = FakeTransport(typesafe=[httpx.Response(401)], openrouter=[jev_response()])
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    with pytest.raises(LLMRequestError):
        client.decide("state", QUESTIONS)

    assert fake.providers_called == ["typesafe"]


def test_typesafe_rate_limit_then_success() -> None:
    fake = FakeTransport(
        typesafe=[httpx.Response(429), jev_response()],
    )
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.decide("state", QUESTIONS)

    assert result["provider"] == "typesafe"
    assert sleep.waits == [15.0]


def test_decide_records_prompt_ref_in_raw_log() -> None:
    import json

    from app.llm.raw_log import InMemoryRawLogStore, RawCallLogger
    from app.prompts.ref import PromptRef

    store = InMemoryRawLogStore()
    fake = FakeTransport(typesafe=[jev_response()])
    client = JevClient(
        make_settings(),
        transport=httpx.MockTransport(fake),
        sleep=FakeSleep(),
        raw_logger=RawCallLogger(store),
    )
    ref = PromptRef(key="jev.search_tag_score", version=3, checksum="abc123")

    client.decide("state", QUESTIONS, prompt_ref=ref)

    payloads = [json.loads(raw) for raw in store.objects.values()]
    assert len(payloads) == 1
    assert payloads[0]["prompt"] == {
        "key": "jev.search_tag_score",
        "version": 3,
        "checksum": "abc123",
    }
