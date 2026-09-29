from app.config import Settings


def test_llm_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.evren_model == "deepseek-v4.1-flash"
    assert settings.openrouter_model == "deepseek/deepseek-v4.1-flash"
    assert settings.jev_model == "jev-latest"
    assert settings.llm_request_timeout_s == 180.0
    assert settings.llm_max_tokens == 16000
    assert settings.llm_retry_waits_s == [15, 30, 60, 120]
    assert settings.llm_max_total_wait_s == 600.0
    assert settings.llm_transient_attempts == 2


def test_retry_waits_parsed_from_comma_separated_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_RETRY_WAITS_S", "5,10,20")

    settings = Settings(_env_file=None)

    assert settings.llm_retry_waits_s == [5, 10, 20]


def test_model_names_come_from_env(monkeypatch) -> None:
    monkeypatch.setenv("EVREN_MODEL", "custom-model")

    settings = Settings(_env_file=None)

    assert settings.evren_model == "custom-model"
