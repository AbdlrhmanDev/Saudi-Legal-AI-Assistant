"""Environment for tests and the evaluation harness. Import this before any legal_core module:
configuration and the database engine are cached on first use."""

import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 12)  # pinned "today" so results do not depend on the wall clock


def _test_database_url() -> str:
    from sqlalchemy.engine import make_url

    if os.environ.get("TEST_DATABASE_URL"):
        url = make_url(os.environ["TEST_DATABASE_URL"])
        if not (url.database or "").endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL must target a database whose name ends with `_test`.")
        return url.render_as_string(hide_password=False)
    from legal_core.config import Settings

    url = make_url(Settings().database_url)
    database = url.database or "legal_ai"
    if not database.endswith("_test"):
        url = url.set(database=f"{database}_test")
    return url.render_as_string(hide_password=False)


def configure(*, offline_models: bool = True) -> None:
    os.chdir(ROOT)
    os.environ["DATABASE_URL"] = _test_database_url()
    os.environ.update(
        {
            "ENVIRONMENT": "test",
            "JWT_SECRET": "test-secret-not-used-anywhere-else-0123456789",
            "ADMIN_EMAIL": "admin@example.test",
            "ADMIN_PASSWORD": "test-admin-password",
            "FETCH_BACKOFF_SECONDS": "0",
            "TRACE_STORE_QUERY_TEXT": "false",
            "ALLOW_FIXTURE_SOURCES": "true",
            "RATE_LIMIT_CHAT_PER_MINUTE": "1000",
            "RATE_LIMIT_LOGIN_PER_MINUTE": "1000",
            "FETCH_MIN_INTERVAL_SECONDS": "0",
            "SYNC_RETRY_BACKOFF_SECONDS": "0",
            # Caches are exercised by dedicated tests; elsewhere every call must hit the database.
            "ANSWER_CACHE_SIZE": "0",
            "FRESHNESS_CACHE_SECONDS": "0",
        }
    )
    if offline_models:
        os.environ.update({"EMBEDDING_PROVIDER": "hash", "LLM_PROVIDER": "extractive"})


def _ensure_database() -> None:
    """Create the test database on first use so a clean checkout needs no manual step."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import OperationalError

    from legal_core.db import get_engine

    try:
        with get_engine().connect():
            return
    except OperationalError as exc:
        if "does not exist" not in str(exc.orig):
            raise
    url = make_url(os.environ["DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    get_engine().dispose()


def reset_schema() -> None:
    """Drop everything in the test database and rebuild it from the migrations."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from legal_core.db import get_engine

    _ensure_database()
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
