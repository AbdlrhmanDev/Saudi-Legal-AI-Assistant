from functools import lru_cache
from pathlib import Path

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

    # Auth
    jwt_secret: str = _INSECURE_SECRET
    jwt_ttl_minutes: int = 480
    admin_email: str = "admin@example.test"
    admin_password: str | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_chat_per_minute: int = 20
    rate_limit_login_per_minute: int = 10
    password_reset_ttl_minutes: int = 30

    # Models. "hash" / "extractive" are deterministic offline providers used for
    # tests and key-less local runs; they are not a substitute for real models.
    embedding_provider: str = "hash"  # hash | openai
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    llm_provider: str = "extractive"  # extractive | openai
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    # Retrieval
    retrieval_top_k: int = 5
    retrieval_candidate_k: int = 30
    rrf_k: int = 60
    min_lexical_coverage: float = 0.34
    min_vector_similarity: float = 0.45
    max_article_chars: int = 1800
    max_evidence_chars: int = 4000

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
    def allowlisted_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.source_allowlist.split(",") if h.strip()}

    def assert_safe_for_environment(self) -> None:
        if self.environment == "production":
            if self.jwt_secret == _INSECURE_SECRET or len(self.jwt_secret) < 32:
                raise RuntimeError("JWT_SECRET must be a strong secret in production")
            if "*" in self.cors_origin_list:
                raise RuntimeError("Wildcard CORS is not allowed in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
