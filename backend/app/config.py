from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

EVREN_BASE_URL_DEFAULT = "https://evren-llmapi.ssyz.org.tr/v1"
OPENROUTER_BASE_URL_DEFAULT = "https://openrouter.ai/api/v1"
TYPESAFE_BASE_URL_DEFAULT = "https://api.typesafe.ai/v1"


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
    evren_base_url: str = EVREN_BASE_URL_DEFAULT
    evren_model: str = "deepseek-v4.1-flash"
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = OPENROUTER_BASE_URL_DEFAULT
    openrouter_model: str = "deepseek/deepseek-v4.1-flash"
    typesafe_api_key: SecretStr | None = None
    typesafe_base_url: str = TYPESAFE_BASE_URL_DEFAULT
    jev_model: str = "jev-latest"

    llm_request_timeout_s: float = 180.0
    llm_max_tokens: int = 16000
    llm_retry_waits_s: Annotated[list[int], NoDecode] = [15, 30, 60, 120]
    llm_max_total_wait_s: float = 600.0
    llm_transient_attempts: int = 2

    @field_validator("llm_retry_waits_s", mode="before")
    @classmethod
    def _parse_retry_waits(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    postgres_host: str | None = None
    postgres_port: str | None = None
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: SecretStr | None = None

    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None
    # Graph migrations are off by default; enabled together with the `graph`
    # compose profile (Phase 3). When false the migrator skips Neo4j entirely.
    neo4j_enabled: bool = False

    redis_url: str | None = None

    # TÜİK databrowser2 connector (measured 2026-09-30: at most 2 concurrent
    # requests; 3+ parallel requests can answer HTTP 200 with a throttle page).
    tuik_max_concurrency: int = 2
    tuik_request_timeout_s: float = 30.0
    tuik_max_retries: int = 3
    tuik_retry_backoff_s: float = 1.0
    tuik_throttle_wait_s: float = 5.0

    # TÜİK nsiws SDMX REST backup channel (measured 2026-09-30: one worker is
    # enough; ~25% of requests silently 30 s timeout and need a retry; the
    # Keycloak token lives ~5 min, so refresh ~1 min early and once on a 401).
    tuik_api_key: SecretStr | None = None
    tuik_nsiws_base_url: str = "https://nsiws.tuik.gov.tr/rest"
    tuik_nsiws_token_url: str = "https://giris.tuik.gov.tr/realms/web/protocol/openid-connect/token"
    tuik_nsiws_client_id: str = "nsi-ws-consumer"
    tuik_nsiws_max_concurrency: int = 1
    tuik_nsiws_request_timeout_s: float = 30.0
    tuik_nsiws_max_retries: int = 3
    tuik_nsiws_retry_backoff_s: float = 1.0
    tuik_nsiws_token_refresh_skew_s: float = 60.0

    # TÜİK Turcat (IMF SDDS national summary page). Measured 2026-09-30: no WAF
    # and no throttle page, so it can use up to 4 workers.
    turcat_max_concurrency: int = 4
    turcat_request_timeout_s: float = 30.0
    turcat_max_retries: int = 3
    turcat_retry_backoff_s: float = 1.0

    # TÜİK CİP (Coğrafi İstatistik Portalı) connector. Measured 2026-09-30: a
    # 40-request burst saw no throttling, so up to 4 workers are safe.
    cip_max_concurrency: int = 4
    cip_request_timeout_s: float = 30.0
    cip_max_retries: int = 3
    cip_retry_backoff_s: float = 1.0

    # TÜİK classification server (siniflama.tuik.gov.tr). Measured 2026-09-30:
    # token-free, no WAF; 1, 2 and 4 parallel requests all answered (median
    # 1.0-1.5 s). The largest tree is ~10 MB / ~20 s and the largest
    # correspondence detail took 53 s, so the timeout is far above databrowser2's.
    siniflama_base_url: str = "https://siniflama.tuik.gov.tr"
    siniflama_max_concurrency: int = 2
    siniflama_request_timeout_s: float = 120.0
    siniflama_max_retries: int = 3
    siniflama_retry_backoff_s: float = 1.0

    # TÜİK bi.tuik Qlik Sense foreign-trade engine (GTS + ÖTS). Measured
    # 2026-09-30: anonymous session, no login; the engine answers no pings while
    # computing a large cube, so pings are disabled and one session is used at a
    # time. A wide cube page (one month x TARIFE8 x ULKE x IHRITH = 254,344 rows)
    # is ~60 s, hence the generous per-call timeout.
    tuik_bi_base_url: str = "https://bi.tuik.gov.tr"
    tuik_bi_gts_app_id: str = "bd4b4757-a3c9-45ba-b4fb-5c8d7e2d2c42"
    tuik_bi_ots_app_id: str = "8db826a9-59f2-4a33-a91e-88ca417dddf9"
    tuik_bi_request_timeout_s: float = 300.0
    tuik_bi_open_timeout_s: float = 60.0
    tuik_bi_page_cells: int = 10_000
    tuik_bi_max_size_bytes: int = 64 * 1024 * 1024
    tuik_bi_max_retries: int = 3
    tuik_bi_retry_backoff_s: float = 2.0

    # TÜİK Turizm İstatistikleri (biruni.tuik.gov.tr/turizmapp), a legacy ZK
    # "DHTML" AU application (cikis/giris/sinir). Measured live 2026-10-01: the
    # front silently stalls requests closer than ~2 s apart (0.2 s hangs until
    # the timeout), so requests are spaced at least ``pause_s``. Timeouts are
    # generous because a wide report can take a while to render.
    tuik_turizm_base_url: str = "https://biruni.tuik.gov.tr/turizmapp"
    tuik_turizm_request_timeout_s: float = 90.0
    tuik_turizm_max_retries: int = 3
    tuik_turizm_retry_backoff_s: float = 2.0
    tuik_turizm_pause_s: float = 2.0
    tuik_turizm_stall_s: float = 30.0
    tuik_turizm_max_parallel_sessions: int = 1

    # TÜİK Seçim İstatistikleri (biruni.tuik.gov.tr/secimdagitimapp), the legacy
    # ZK "DHTML" application serving the parliamentary election results. Same old
    # AU dialect as turizmapp: the front silently stalls requests closer than
    # ~2 s, so they are spaced at least ``pause_s`` apart. Two parallel sessions
    # are allowed (pacing is per session).
    tuik_secim_base_url: str = "https://biruni.tuik.gov.tr/secimdagitimapp"
    tuik_secim_request_timeout_s: float = 90.0
    tuik_secim_max_retries: int = 3
    tuik_secim_retry_backoff_s: float = 2.0
    tuik_secim_pause_s: float = 2.0
    tuik_secim_stall_s: float = 30.0
    tuik_secim_max_parallel_sessions: int = 2

    # TÜİK Biruni Yayın Sistemi (biruni.tuik.gov.tr/yayin), a ZK 7 AU catalogue
    # of publications/reports/micro-data sets. Measured 2026-10-01: bootstrap
    # ~50 KB / ~0.5 s, each AU page ~18 KB; the front protection silently stalls
    # requests that arrive too fast, so requests are spaced at least ``pause_s``
    # apart. Timeouts and retries follow the other TÜİK channels.
    tuik_yayin_base_url: str = "https://biruni.tuik.gov.tr"
    tuik_yayin_request_timeout_s: float = 30.0
    tuik_yayin_max_retries: int = 3
    tuik_yayin_retry_backoff_s: float = 2.0
    tuik_yayin_pause_s: float = 1.0
    # A request slower than this is reported as a stall (the silent-throttle
    # symptom) even when it eventually returns.
    tuik_yayin_stall_s: float = 20.0

    # TÜİK Veri Portalı (veriportali.tuik.gov.tr). Measured live 2026-10-02:
    # the WAF only inspects headers, so every JSON request must carry the browser
    # User-Agent plus ``X-Requested-With: XMLHttpRequest`` (no UA -> 403; UA
    # without XHR -> 404 text/plain on JSON paths). JSON APIs showed no
    # throttling up to 8 parallel, so one worker with no fixed pause is enough;
    # a 200 text/html "Yönlendiriliyor" page still means throttling.
    tuik_veriportali_base_url: str = "https://veriportali.tuik.gov.tr"
    tuik_veriportali_press_base_url: str = "https://www.tuik.gov.tr"
    tuik_veriportali_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )
    tuik_veriportali_max_concurrency: int = 1
    tuik_veriportali_request_timeout_s: float = 30.0
    tuik_veriportali_max_retries: int = 3
    tuik_veriportali_retry_backoff_s: float = 1.0
    tuik_veriportali_throttle_wait_s: float = 5.0

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
