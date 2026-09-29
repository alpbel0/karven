from app.integration_safety import validate_integration_env

GOOD_ENV = {
    "APP_ENV": "test",
    "POSTGRES_DB": "karven_test",
    "POSTGRES_PORT": "25432",
    "NEO4J_URI": "bolt://127.0.0.1:27687",
    "REDIS_URL": "redis://127.0.0.1:26379/0",
    "MINIO_ENDPOINT": "http://127.0.0.1:29000",
}


def test_valid_test_environment_is_accepted() -> None:
    assert validate_integration_env(GOOD_ENV) == []


def test_live_like_environment_is_rejected() -> None:
    live_like = {
        "APP_ENV": "development",
        "POSTGRES_DB": "karven",
        "POSTGRES_PORT": "15432",
        "NEO4J_URI": "bolt://127.0.0.1:17687",
        "REDIS_URL": "redis://127.0.0.1:16379/0",
        "MINIO_ENDPOINT": "http://127.0.0.1:19000",
    }

    problems = validate_integration_env(live_like)

    assert len(problems) == 6
    assert any("APP_ENV" in problem for problem in problems)
    assert any("POSTGRES_DB" in problem for problem in problems)
    assert any("POSTGRES_PORT" in problem for problem in problems)
    assert any("NEO4J_URI" in problem for problem in problems)
    assert any("REDIS_URL" in problem for problem in problems)
    assert any("MINIO_ENDPOINT" in problem for problem in problems)


def test_empty_environment_is_rejected() -> None:
    problems = validate_integration_env({})

    assert len(problems) == 6


def test_partial_mismatch_is_reported() -> None:
    broken = dict(GOOD_ENV, POSTGRES_DB="karven")

    problems = validate_integration_env(broken)

    assert len(problems) == 1
    assert "POSTGRES_DB" in problems[0]


def test_unparseable_ports_are_rejected() -> None:
    broken = dict(GOOD_ENV, POSTGRES_PORT="not-a-port", NEO4J_URI="not-a-url")

    problems = validate_integration_env(broken)

    assert len(problems) == 2
