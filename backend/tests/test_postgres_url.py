from pydantic import SecretStr

from app.config import build_postgres_url
from app.db.base import Base


def test_build_postgres_url_encodes_credentials() -> None:
    url = build_postgres_url(
        host="db.internal",
        port="5432",
        database="karven",
        user="karven",
        password=SecretStr("p@ss:word/1"),
    )

    assert url == "postgresql+psycopg://karven:p%40ss%3Aword%2F1@db.internal:5432/karven"


def test_build_postgres_url_without_password() -> None:
    url = build_postgres_url("localhost", "5432", "karven", "karven", None)

    assert url == "postgresql+psycopg://karven:@localhost:5432/karven"


def test_base_metadata_starts_empty() -> None:
    assert list(Base.metadata.tables) == []
