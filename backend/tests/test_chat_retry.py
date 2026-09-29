import httpx
import pytest

from app.llm.chat import ChatClient
from app.llm.errors import LLMAllProvidersFailed, LLMRequestError
from app.llm.policy import parse_retry_after
from tests.llm_helpers import FakeSleep, FakeTransport, chat_response, make_settings

MESSAGES = [{"role": "user", "content": "hello"}]


def make_client(fake: FakeTransport, sleep: FakeSleep, **overrides):
    settings = make_settings(**overrides)
    return ChatClient(
        settings,
        transport=httpx.MockTransport(fake),
        sleep=sleep,
    )


def test_rate_limit_then_success_without_retry_after() -> None:
    fake = FakeTransport(evren=[httpx.Response(429), httpx.Response(429), chat_response()])
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.complete(MESSAGES)

    assert result.provider == "evren"
    assert result.content == "ok"
    assert sleep.waits == [15.0, 30.0]


def test_rate_limit_honours_retry_after_header() -> None:
    fake = FakeTransport(evren=[httpx.Response(429, headers={"Retry-After": "7"}), chat_response()])
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.complete(MESSAGES)

    assert result.provider == "evren"
    assert sleep.waits == [7.0]


def test_transient_errors_twice_fall_back_to_openrouter() -> None:
    fake = FakeTransport(
        evren=[httpx.Response(500), httpx.Response(503)],
        openrouter=[chat_response(content="from-openrouter")],
    )
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.complete(MESSAGES)

    assert result.provider == "openrouter"
    assert result.content == "from-openrouter"
    assert fake.providers_called == ["evren", "evren", "openrouter"]


def test_transport_error_retries_and_falls_back() -> None:
    fake = FakeTransport(
        evren=[httpx.ConnectError("boom"), httpx.ReadTimeout("slow")],
        openrouter=[chat_response(content="from-openrouter")],
    )
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.complete(MESSAGES)

    assert result.provider == "openrouter"
    assert fake.providers_called == ["evren", "evren", "openrouter"]


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_fatal_status_raises_without_fallback(status: int) -> None:
    fake = FakeTransport(evren=[httpx.Response(status)], openrouter=[chat_response()])
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    with pytest.raises(LLMRequestError):
        client.complete(MESSAGES)

    assert fake.providers_called == ["evren"]


def test_rate_limit_beyond_total_wait_switches_provider() -> None:
    fake = FakeTransport(
        evren=[httpx.Response(429), httpx.Response(429)],
        openrouter=[chat_response(content="fallback")],
    )
    sleep = FakeSleep()
    client = make_client(
        fake,
        sleep,
        llm_max_total_wait_s=20,
        llm_retry_waits_s=[15, 30, 60, 120],
    )

    result = client.complete(MESSAGES)

    assert result.provider == "openrouter"
    assert sleep.waits == [15.0]
    assert fake.providers_called == ["evren", "evren", "openrouter"]


def test_all_providers_failed_attaches_errors() -> None:
    fake = FakeTransport(
        evren=[httpx.Response(500), httpx.Response(500)],
        openrouter=[httpx.Response(500), httpx.Response(500)],
    )
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    with pytest.raises(LLMAllProvidersFailed) as excinfo:
        client.complete(MESSAGES)

    assert [name for name, _error in excinfo.value.errors] == ["evren", "openrouter"]


def test_parse_retry_after_supports_http_date() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not-a-date") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_evren_credit_exhausted_falls_back_to_openrouter() -> None:
    fake = FakeTransport(
        evren=[httpx.Response(402)],
        openrouter=[chat_response(content="from-openrouter")],
    )
    sleep = FakeSleep()
    client = make_client(fake, sleep)

    result = client.complete(MESSAGES)

    assert result.provider == "openrouter"
    assert fake.providers_called == ["evren", "openrouter"]
    assert sleep.waits == []


def test_rate_limit_is_logged(caplog) -> None:
    fake = FakeTransport(evren=[httpx.Response(429), chat_response(content="ok")])
    client = make_client(fake, FakeSleep())

    with caplog.at_level("WARNING", logger="app.llm.policy"):
        client.complete(MESSAGES)

    assert "llm rate limited 429: provider=evren" in caplog.text
