"""Stage or approve a small, real official-law baseline used by the public demo.

The repository never stores copies of the legal texts.  This operator command fetches the
current consolidated Arabic pages from the Saudi Bureau of Experts, stores immutable database
snapshots, parses them, and optionally approves/indexes them after deterministic checks.

Usage:
    python scripts/import_official_baseline.py
    python scripts/import_official_baseline.py --approve
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select

from legal_core.config import get_settings
from legal_core.db import session_scope
from legal_core.embeddings import get_embedder
from legal_core.ingestion import DocumentMetadata, ingest_document
from legal_core.legal_dates import resolve_effective_date
from legal_core.models import DocumentVersion, LegalInstrument, SourceRegistry, UpdateCandidate
from legal_core.monitor import canonicalize_url, make_http_fetcher
from legal_core.parser import document_text, parse_boe_html
from legal_core.review import approve_candidate
from legal_core.snapshots import store_snapshot


@dataclass(frozen=True)
class OfficialDocument:
    title: str
    url: str
    domain: str
    published_at: date
    expected_effective_from: date
    minimum_articles: int


DOCUMENTS = (
    OfficialDocument(
        title="نظام المرور",
        url="https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/85364e57-c01e-41ba-8def-a9a700f183e9/1",
        domain="other",
        published_at=date(2007, 11, 7),
        expected_effective_from=date(2008, 5, 5),
        minimum_articles=80,
    ),
    OfficialDocument(
        title="نظام مكافحة التستر",
        url="https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/bf9e0aae-6df6-4785-a305-ac2300bd0856/1",
        domain="commercial",
        published_at=date(2020, 8, 28),
        expected_effective_from=date(2021, 2, 24),
        minimum_articles=15,
    ),
)


def _source(session) -> SourceRegistry:
    source = session.scalars(
        select(SourceRegistry).where(SourceRegistry.name == "Bureau of Experts - Saudi Laws Portal")
    ).first()
    if source is None:
        source = SourceRegistry(
            name="Bureau of Experts - Saudi Laws Portal",
            authority="Bureau of Experts at the Council of Ministers",
            feed_url="https://laws.boe.gov.sa/",
            allowlisted_host="laws.boe.gov.sa",
            enabled=False,
            crawl_policy="manual",
            check_interval=360,
            is_test_data=False,
        )
        session.add(source)
        session.flush()
    return source


def _active_version(document: OfficialDocument) -> bool:
    """Return true when this official instrument is already serving the RAG index."""
    with session_scope() as session:
        return (
            session.scalar(
                select(DocumentVersion.id)
                .join(LegalInstrument, LegalInstrument.id == DocumentVersion.instrument_id)
                .where(
                    LegalInstrument.official_title_ar == document.title,
                    DocumentVersion.review_status == "approved",
                    DocumentVersion.index_active.is_(True),
                )
                .limit(1)
            )
            is not None
        )


def import_document(document: OfficialDocument, *, approve: bool, if_missing: bool = False) -> dict:
    if if_missing and _active_version(document):
        return {
            "title": document.title,
            "status": "already_active",
            "duplicate": True,
            "articles": None,
            "effective_from": document.expected_effective_from.isoformat(),
            "source_url": canonicalize_url(document.url),
        }
    settings = get_settings()
    content = make_http_fetcher(settings)(document.url)
    parsed = parse_boe_html(content)
    if parsed is None or len(parsed.articles) < document.minimum_articles:
        raise RuntimeError(f"official structure check failed for {document.title}")
    if "possible_prompt_injection" in parsed.warnings:
        raise RuntimeError(f"source isolation check failed for {document.title}")

    effective = resolve_effective_date(
        notice_text="",
        document_text=document_text(parsed),
        published_at=document.published_at,
        consolidated_amendment=False,
    )
    if effective.value != document.expected_effective_from or not effective.evidence:
        raise RuntimeError(
            f"effective-date check failed for {document.title}: "
            f"expected {document.expected_effective_from}, found {effective.value}"
        )

    with session_scope() as session:
        source = _source(session)
        canonical_url = canonicalize_url(document.url)
        snapshot = store_snapshot(
            session,
            content,
            canonical_url=canonical_url,
            source_id=source.id,
            content_type="text/html",
            published_at=document.published_at,
            is_test_data=False,
        )
        result = ingest_document(
            session,
            content=content,
            filename=f"{document.title}.html",
            mime_type="text/html",
            meta=DocumentMetadata(
                title_ar=document.title,
                domain=document.domain,
                instrument_type="law",
                issuing_authority="هيئة الخبراء بمجلس الوزراء",
                source_url=canonical_url,
                published_at=document.published_at,
                effective_from=document.expected_effective_from,
                is_test_data=False,
            ),
            settings=settings,
            snapshot_id=snapshot.id,
        )
        version = session.get(DocumentVersion, result.version_id)
        candidate = session.get(UpdateCandidate, result.candidate_id) if result.candidate_id else None
        status = version.review_status if version else "missing"
        if approve and version and version.review_status != "approved":
            if candidate is None:
                raise RuntimeError(f"review candidate missing for {document.title}")
            evidence = effective.evidence[0]
            approve_candidate(
                session,
                candidate.id,
                actor_id=None,
                effective_from=document.expected_effective_from,
                published_at=document.published_at,
                effective_date_evidence=(
                    f"النص الرسمي المنشور في بوابة الأنظمة: {evidence.span}; "
                    f"تاريخ النشر {document.published_at.isoformat()}"
                ),
                reason="Official baseline import requested by the project owner",
                acknowledge_warnings=False,
                embedder=get_embedder(settings),
                settings=settings,
            )
            status = "approved"
        return {
            "title": document.title,
            "status": status,
            "duplicate": result.duplicate,
            "articles": result.article_count,
            "effective_from": document.expected_effective_from.isoformat(),
            "source_url": canonical_url,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approve", action="store_true", help="approve and add the checked texts to the active RAG index")
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="skip each official instrument that already has an approved active version",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            [import_document(doc, approve=args.approve, if_missing=args.if_missing) for doc in DOCUMENTS],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
