"""Operational commands.

    python -m legal_core.cli create-admin      # from ADMIN_EMAIL / ADMIN_PASSWORD
    python -m legal_core.cli seed-sources      # official source registry (disabled) + fixture feed
    python -m legal_core.cli seed-fixtures     # ingest + approve the fictional TEST corpus
    python -m legal_core.cli bootstrap         # admin + official source registry (no test data)
    python -m legal_core.cli bootstrap-demo    # bootstrap + fictional corpus (set ALLOW_FIXTURE_SOURCES=true
                                               # to also get the simulated feed)
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings, get_settings
from legal_core.db import session_scope
from legal_core.embeddings import get_embedder
from legal_core.ingestion import DocumentMetadata, ingest_document
from legal_core.models import SourceRegistry, User
from legal_core.review import approve_candidate

# Starting points from the PRD reference list. They are registered DISABLED: enabling one is an
# explicit admin decision after reviewing that source's access and reuse terms.
OFFICIAL_SOURCES = [
    ("Umm Al-Qura RSS - Decisions & Regulations", "Umm Al-Qura (Official Gazette)", "https://www.uqn.gov.sa/rssFeed/21", "www.uqn.gov.sa", "feed_only"),
    ("Bureau of Experts - Saudi Laws Portal", "Bureau of Experts at the Council of Ministers", "https://laws.boe.gov.sa/", "laws.boe.gov.sa", "manual"),
    ("HRSD - Labor Law", "Ministry of Human Resources and Social Development", "https://www.hrsd.gov.sa/", "www.hrsd.gov.sa", "manual"),
    ("Ministry of Commerce - Regulations", "Ministry of Commerce", "https://mc.gov.sa/ar/Regulations/pages/default.aspx", "mc.gov.sa", "manual"),
    ("SDAIA - PDPL Knowledge Center", "Saudi Data & AI Authority", "https://dgp.sdaia.gov.sa/wps/portal/pdp/knowledgecenter/", "dgp.sdaia.gov.sa", "manual"),
]  # fmt: skip
FIXTURE_SOURCE = "TEST FIXTURE - simulated official feed"


def create_admin(session: Session, settings: Settings) -> str:
    if not settings.admin_password:
        raise SystemExit("ADMIN_PASSWORD is not set")
    if len(settings.admin_password) < 12:
        raise SystemExit("ADMIN_PASSWORD must be at least 12 characters")
    email = settings.admin_email.lower()
    user = session.scalars(select(User).where(User.email == email)).first()
    password_hash = bcrypt.hashpw(settings.admin_password.encode()[:72], bcrypt.gensalt()).decode("ascii")
    if user:
        user.password_hash, user.role = password_hash, "admin"
        return f"admin updated: {email}"
    session.add(User(email=email, role="admin", password_hash=password_hash))
    return f"admin created: {email}"


def seed_sources(session: Session, settings: Settings) -> str:
    existing = set(session.scalars(select(SourceRegistry.name)))
    added = 0
    for name, authority, url, host, policy in OFFICIAL_SOURCES:
        if name not in existing:
            session.add(
                SourceRegistry(name=name, authority=authority, feed_url=url, allowlisted_host=host,
                               enabled=False, crawl_policy=policy, check_interval=settings.monitor_interval_minutes)
            )  # fmt: skip
            added += 1
    if settings.allow_fixture_sources and FIXTURE_SOURCE not in existing:
        session.add(
            SourceRegistry(name=FIXTURE_SOURCE, authority="TEST DATA (not an official source)",
                           feed_url="feeds/simulated_feed.xml", allowlisted_host="fixture.invalid",
                           enabled=True, crawl_policy="fixture", check_interval=60, is_test_data=True)
        )  # fmt: skip
        added += 1
    return f"sources added: {added}"


def seed_fixtures(session: Session, settings: Settings) -> str:
    """Load the fictional corpus through the real ingestion + review path."""
    manifest = json.loads((settings.fixtures_dir / "corpus" / "manifest.json").read_text(encoding="utf-8"))
    embedder = get_embedder(settings)
    loaded = skipped = 0
    for doc in manifest["documents"]:
        if not doc.get("seed", True):
            continue
        path = settings.fixtures_dir / "corpus" / doc["file"]
        result = ingest_document(
            session,
            content=path.read_bytes(),
            filename=Path(doc["file"]).name,
            mime_type="text/plain",
            settings=settings,
            meta=DocumentMetadata(
                title_ar=doc["title_ar"],
                domain=doc["domain"],
                instrument_type=doc["instrument_type"],
                issuing_authority=doc["issuing_authority"],
                source_url=doc["source_url"],
                version_label=doc.get("version_label"),
                published_at=date.fromisoformat(doc["published_at"]),
                is_test_data=True,
            ),
        )
        if result.duplicate:
            skipped += 1
            continue
        approve_candidate(
            session,
            result.candidate_id,
            actor_id=None,
            effective_from=date.fromisoformat(doc["effective_from"]),
            effective_date_evidence="Fixture manifest (fictional test data, seeded by CLI).",
            reason="seed-fixtures",
            acknowledge_warnings=True,  # the adversarial fixture is loaded on purpose
            embedder=embedder,
            settings=settings,
        )
        loaded += 1
    return f"fixture documents approved: {loaded}, already present: {skipped}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="legal_core.cli")
    parser.add_argument("command", choices=["create-admin", "seed-sources", "seed-fixtures", "bootstrap", "bootstrap-demo"])
    args = parser.parse_args(argv)
    settings = get_settings()
    steps = {
        "create-admin": [create_admin],
        "seed-sources": [seed_sources],
        "seed-fixtures": [seed_fixtures],
        "bootstrap": [create_admin, seed_sources],
        "bootstrap-demo": [create_admin, seed_sources, seed_fixtures],
    }[args.command]
    for step in steps:
        with session_scope() as session:
            print(step(session, settings))


if __name__ == "__main__":
    sys.exit(main())
