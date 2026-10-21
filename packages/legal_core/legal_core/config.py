from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://legal:legal@127.0.0.1:5432/legal_ai"

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_driver(cls, value: object) -> object:
        """Use the installed psycopg v3 driver for provider-style PostgreSQL URLs."""
        if isinstance(value, str) and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        if isinstance(value, str) and value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        return value

    # Database pool, per process. Total connections = (pool + overflow) x processes x replicas;
    # keep that under the server's max_connections (or put PgBouncer in front).
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: float = 10.0
    db_pool_recycle_seconds: int = 1800
    # Set when DATABASE_URL points at PgBouncer in transaction-pooling mode (API only; the worker's
    # advisory lock needs a direct session connection).
    db_pgbouncer: bool = False

    # API concurrency: sync endpoints run in a thread pool of this size per process.
    api_thread_limit: int = 100
    # Reverse proxies in front of the API (Railway/Render/nginx = 1). The client address is taken
    # from X-Forwarded-For that many hops from the right; 0 trusts no header.
    trusted_proxy_count: int = 0

    # Auth
    jwt_secret: str = _INSECURE_SECRET
    jwt_ttl_minutes: int = 480
    admin_email: str = "admin@example.test"
    admin_password: str | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_chat_per_minute: int = 20
    rate_limit_login_per_minute: int = 10
    # "database" shares counters across processes and replicas; "memory" is per process.
    rate_limit_backend: str = "database"
    password_reset_ttl_minutes: int = 30

    # Models. "hash" / "extractive" are deterministic offline providers used for
    # tests and key-less local runs; they are not a substitute for real models.
    embedding_provider: str = "hash"  # hash | openai
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    llm_provider: str = "extractive"  # extractive | openai
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 2
    # Classification helper for the nightly agent: "rules" (deterministic) | "openai".
    agent_provider: str = "rules"
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    # Retrieval
    retrieval_top_k: int = 5
    retrieval_candidate_k: int = 30
    rrf_k: int = 60
    min_lexical_coverage: float = 0.34
    # One generic legal word (for example "violations") must not make an unrelated passage
    # answerable. Exact article+instrument hints and semantic retrieval have separate gates.
    min_lexical_matches: int = 2
    min_vector_similarity: float = 0.45
    max_article_chars: int = 1800
    max_evidence_chars: int = 4000
    # In-process answer cache, keyed by the active index generation (0 disables it).
    answer_cache_size: int = 2000
    answer_cache_ttl_seconds: int = 900
    # Public Ministry of Justice/Najiz service directory. Queried live; never stored locally.
    official_service_search_enabled: bool = True
    official_service_timeout_seconds: float = 25.0

    # Storage / uploads
    storage_dir: Path = Path("var/storage")
    fixtures_dir: Path = Path("fixtures")
    max_upload_bytes: int = 20 * 1024 * 1024

    # Source monitoring
    source_allowlist: str = (
        "laws.boe.gov.sa,www.uqn.gov.sa,uqn.gov.sa,www.hrsd.gov.sa,hrsd.gov.sa,mc.gov.sa,dgp.sdaia.gov.sa"
    )
    allow_fixture_sources: bool = False  # test/demo only: simulated feed + fictional corpus
    monitor_interval_minutes: int = 360
    http_user_agent: str = "SaudiLegalAI-MVP/0.1 (technical assessment; polite monitor)"
    fetch_timeout_seconds: float = 15.0
    fetch_max_attempts: int = 3
    fetch_backoff_seconds: float = 2.0
    fetch_min_interval_seconds: float = 1.0  # politeness delay between requests to one host
    respect_robots_txt: bool = True

    # Nightly synchronization (PRD v2 section 6)
    sync_timezone: str = "Asia/Riyadh"
    sync_hour: int = 0
    sync_minute: int = 0
    sync_catchup_days: int = 3  # missed scheduled runs within this window are caught up
    sync_max_retries: int = 3
    sync_retry_backoff_seconds: float = 300.0
    sync_max_candidates_per_run: int = 200
    sync_max_embedding_inputs_per_run: int = 5000
    # A source with no successful check for this long is reported as delayed / degraded.
    freshness_delayed_hours: int = 36
    freshness_degraded_hours: int = 72
    freshness_cache_seconds: int = 30
    alert_webhook_url: str | None = None

    # Worker
    worker_poll_seconds: float = 3.0
    worker_heartbeat_seconds: float = 15.0
    worker_stale_seconds: float = 90.0
    job_max_retries: int = 2

    # Privacy
    trace_store_query_text: bool = False
    trace_retention_days: int = 30

    @property
    def expose_reset_token(self) -> bool:
        """No mail service is wired in. Outside production the reset token is returned to the
        caller so the flow can be demonstrated; in production it is never returned."""
        return self.environment in ("development", "test")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sync_tz(self) -> ZoneInfo:
        return ZoneInfo(self.sync_timezone)

    @property
    def allowlisted_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.source_allowlist.split(",") if h.strip()}

    def assert_safe_for_environment(self) -> None:
        if self.environment == "production":
            if self.jwt_secret == _INSECURE_SECRET or len(self.jwt_secret) < 32:
                raise RuntimeError("JWT_SECRET must be a strong secret in production")
            if not self.admin_password or len(self.admin_password) < 12:
                raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters in production")
            if self.admin_email.lower().endswith((".test", "@example.com", "@example.test")):
                raise RuntimeError("ADMIN_EMAIL must be a real private address in production")
            origins = self.cors_origin_list
            if not origins or "*" in origins:
                raise RuntimeError("CORS_ORIGINS must contain explicit HTTPS origins in production")
            if any(not origin.startswith("https://") for origin in origins):
                raise RuntimeError("Every production CORS origin must use HTTPS")
            if self.allow_fixture_sources:
                raise RuntimeError("ALLOW_FIXTURE_SOURCES must be false in production")
            if self.embedding_provider != "openai" or self.llm_provider != "openai":
                raise RuntimeError(
                    "Production requires semantic retrieval and grounded generation: "
                    "set EMBEDDING_PROVIDER=openai and LLM_PROVIDER=openai"
                )
            if not self.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is required for production semantic retrieval")


@lru_cache
def get_settings() -> Settings:
    return Settings()
