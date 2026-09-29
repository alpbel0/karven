from app.config import Settings


def test_settings_load_without_env_file(monkeypatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("EVREN_API_KEY", raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.evren_api_key is None


def test_settings_pick_up_environment_variable(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("EVREN_API_KEY", "secret-value")

    settings = Settings(_env_file=None)

    assert settings.app_env == "test"
    assert settings.evren_api_key is not None
    assert settings.evren_api_key.get_secret_value() == "secret-value"
