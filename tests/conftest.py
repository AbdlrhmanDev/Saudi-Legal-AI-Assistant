"""Test environment. Integration tests need PostgreSQL + pgvector and use a separate
database (default: the configured database name with a `_test` suffix)."""

import shutil

from tests._env import ROOT, TODAY, configure, reset_schema

configure()

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from legal_core.config import get_settings  # noqa: E402
from legal_core.db import get_engine, get_sessionmaker  # noqa: E402
from legal_core.embeddings import get_embedder  # noqa: E402
from legal_core.llm import get_answer_model  # noqa: E402
from legal_core.models import Base  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    settings = get_settings()
    settings.storage_dir = ROOT / "var" / "test-storage"
    shutil.rmtree(settings.storage_dir, ignore_errors=True)
    yield settings
    shutil.rmtree(settings.storage_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def embedder(settings):
    return get_embedder(settings)


@pytest.fixture(scope="session")
def llm(settings):
    return get_answer_model(settings)


@pytest.fixture(scope="session")
def migrated_engine():
    try:
        reset_schema()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL test database is not reachable: {exc.orig}")
    return get_engine()


@pytest.fixture
def db(migrated_engine):
    """A session on a clean database. Tables are truncated before each test."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with migrated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def corpus(db, settings):
    """Fictional fixture corpus, ingested and approved through the real code path."""
    from legal_core.cli import seed_fixtures, seed_sources

    seed_sources(db, settings)
    seed_fixtures(db, settings)
    db.commit()
    return db


@pytest.fixture
def ask(corpus, settings, embedder, llm):
    from legal_core.answer import answer_question

    # Tests always pin the as-of date so results do not depend on the wall clock.
    def _ask(query: str, *, domain: str | None = None, as_of=TODAY) -> dict:
        return answer_question(
            corpus, query=query, domain=domain, as_of_date=as_of, settings=settings, embedder=embedder, llm=llm
        )

    return _ask
