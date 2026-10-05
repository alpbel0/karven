from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from pydantic import SecretStr, field_validator, model_validator
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

    # TCMB EVDS3 (key-less plain JSON, base .../igmevdsms-dis). Measured live
    # 2026-10-03: no throttle seen and the rate limit is unknown, so requests are
    # spaced at least ``tcmb_request_interval_s`` apart with one worker.
    tcmb_base_url: str = "https://evds3.tcmb.gov.tr/igmevdsms-dis"
    tcmb_request_interval_s: float = 0.25
    tcmb_request_timeout_s: float = 60.0
    tcmb_max_retries: int = 3
    tcmb_retry_backoff_s: float = 1.0

    # Core refresh engine (Task 1.4c). Calendar-driven: a calendar series is
    # polled on its release date and retried at most every ``core_retry_hours``
    # until the new period lands; a ``no_new_period`` alert opens once the
    # release is ``core_grace_days`` late. A calendar-less daily series (USD) is
    # polled once per weekday after ``core_daily_after`` (Europe/Istanbul) and
    # alerts only when its latest period is older than ``core_daily_stale_days``
    # (long holidays must not alert). ``core_failure_threshold`` consecutive
    # failed/empty refresh jobs open a ``repeated_failure`` alert.
    core_grace_days: int = 2
    core_retry_hours: int = 2
    core_daily_after: str = "16:30"
    core_daily_stale_days: int = 10
    core_failure_threshold: int = 3

    # Core scheduler (Task 1.4d). One purpose-built process loops run_tick every
    # ``core_tick_minutes``; the release calendar is re-synced when its newest
    # ``fetched_at`` is older than ``core_calendar_refresh_hours`` (and the last
    # attempt is older than ``core_retry_hours``). After each tick the process
    # writes an ISO timestamp to ``core_heartbeat_path`` (the compose healthcheck
    # reads its mtime). All three are env-overridable like the other core_* keys.
    core_tick_minutes: int = 15
    core_calendar_refresh_hours: int = 24
    core_heartbeat_path: str = "/tmp/core-scheduler.heartbeat"

    # On-demand fetch (Task 1.5). A running on-demand job writes ``heartbeat_at``
    # every ``fetch_heartbeat_interval_seconds``; the watchdog fails a job only
    # after ``fetch_heartbeat_stall_minutes`` without a beat (there is no fixed
    # 20-minute cap) and runs every ``fetch_watchdog_interval_seconds``. The
    # interval must be well below the stall threshold so a live job is never
    # declared dead. ``fetch_worker_concurrency`` is the Celery worker's
    # ``--concurrency``. All four are env-overridable like the other core_* keys.
    fetch_heartbeat_stall_minutes: int = 5
    fetch_heartbeat_interval_seconds: int = 30
    fetch_watchdog_interval_seconds: int = 60
    fetch_worker_concurrency: int = 2
    # Data-fetch agent (Task 1.6): the number of diagnosis rounds allowed per job
    # chain. The first failed job is round 1; a retry it opens is round 2, which
    # may still be diagnosed but cannot request another retry (round < max_rounds).
    fetch_agent_max_rounds: int = 2

    # News intake (Task 1.7). The beat polls the five feeds every
    # ``news_poll_interval_seconds``; the first time a source is seen only its
    # ``news_first_run_limit`` newest items are taken. Article pages are fetched
    # up to ``news_max_attempts`` times (timeout/transport/5xx only); a page
    # whose extracted text is shorter than ``news_min_text_chars`` is
    # ``no_text``. A ``pending`` row never fetched is requeued once it is older
    # than ``news_pending_requeue_seconds``. All are env-overridable.
    news_poll_interval_seconds: int = 900
    news_first_run_limit: int = 4
    news_request_timeout_s: float = 30.0
    news_max_attempts: int = 3
    news_min_text_chars: int = 200
    news_pending_requeue_seconds: int = 600
    news_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    )

    @model_validator(mode="after")
    def _validate_fetch_settings(self) -> "Settings":
        if self.fetch_heartbeat_stall_minutes <= 0:
            raise ValueError("FETCH_HEARTBEAT_STALL_MINUTES must be positive")
        if self.fetch_heartbeat_interval_seconds <= 0:
            raise ValueError("FETCH_HEARTBEAT_INTERVAL_SECONDS must be positive")
        if self.fetch_watchdog_interval_seconds <= 0:
            raise ValueError("FETCH_WATCHDOG_INTERVAL_SECONDS must be positive")
        if self.fetch_worker_concurrency <= 0:
            raise ValueError("FETCH_WORKER_CONCURRENCY must be positive")
        if self.fetch_agent_max_rounds < 1:
            raise ValueError("FETCH_AGENT_MAX_ROUNDS must be at least 1")
        if self.fetch_heartbeat_interval_seconds >= self.fetch_heartbeat_stall_minutes * 60:
            raise ValueError(
                "FETCH_HEARTBEAT_INTERVAL_SECONDS must be less than "
                "FETCH_HEARTBEAT_STALL_MINUTES * 60"
            )
        return self

    @model_validator(mode="after")
    def _validate_news_settings(self) -> "Settings":
        positive = (
            "news_poll_interval_seconds",
            "news_first_run_limit",
            "news_request_timeout_s",
            "news_max_attempts",
            "news_min_text_chars",
            "news_pending_requeue_seconds",
        )
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive")
        return self

    # Catalog series search (Task 2.4). Jev 1-5 scores pass when
    # p(rating 4) + p(rating 5) >= ``search_pass_probability``; a dimension
    # choice below ``search_low_confidence`` triggers EVREN rewrites. Candidate
    # datasets are scored in chunks of ``search_dataset_chunk`` questions; at most
    # ``search_max_datasets`` datasets and ``search_max_results`` series are
    # returned. A series frequency is only compatible when the Jev yes-probability
    # is at least ``search_frequency_floor``. When nothing is strong, datasets
    # rated at least ``search_near_miss_probability`` are offered as near-miss
    # candidates (at most ``search_near_miss_datasets`` of them).
    search_pass_probability: float = 0.60
    search_low_confidence: float = 0.60
    search_dataset_chunk: int = 50
    search_max_datasets: int = 5
    search_max_results: int = 3
    search_frequency_floor: float = 0.40
    search_near_miss_probability: float = 0.15
    search_near_miss_datasets: int = 2

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
