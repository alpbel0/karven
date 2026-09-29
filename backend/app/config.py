from pathlib import Path
from urllib.parse import quote_plus

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_postgres_url(
    host: str | None,
    port: str | None,
    database: str | None,
    user: str | None,
    password: SecretStr | None,
) -> str:
    """Build a synchronous SQLAlchemy URL for psycopg 3.

    User and password are percent-encoded so special characters survive.
    """
    secret = password.get_secret_value() if password is not None else ""
    return (
        f"postgresql+psycopg://{quote_plus(user or '')}:{quote_plus(secret)}"
        f"@{host or ''}:{port or ''}/{database or ''}"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"

    evren_api_key: SecretStr | None = None
    evren_base_url: str | None = None
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str | None = None
    typesafe_api_key: SecretStr | None = None
    typesafe_base_url: str | None = None

    postgres_host: str | None = None
    postgres_port: str | None = None
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None

    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None

    redis_url: str | None = None

    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: SecretStr | None = None
    minio_bucket_raw: str | None = None

    admin_username: str | None = None
    admin_password_hash: SecretStr | None = None

    @property
    def postgres_url(self) -> str:
        return build_postgres_url(
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
            user=self.postgres_user,
            password=self.postgres_password,
        )


settings = Settings()
