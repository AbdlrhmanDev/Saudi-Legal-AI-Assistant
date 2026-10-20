"""Document ingestion: validate -> extract -> parse -> store an immutable, unreviewed version.

Ingestion never makes content authoritative. Every ingested version lands in state needs_review
and is only indexed for retrieval once the release gate (or a reviewer) publishes it.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.diffing import base_version_for, diff_against_base
from legal_core.models import (
    DOMAINS,
    INSTRUMENT_TYPES,
    Article,
    DocumentVersion,
    LegalInstrument,
    UpdateCandidate,
)
from legal_core.normalize import normalize_ar
from legal_core.parser import (
    document_text,
    extract_text,
    parse_boe_html,
    parse_legal_text,
    parse_uqn_html,
    sniff_kind,
)


class IngestionError(ValueError):
    pass


@dataclass
class DocumentMetadata:
    title_ar: str | None = None
    domain: str | None = None
    issuing_authority: str | None = None
    instrument_type: str | None = None
    source_url: str | None = None
    version_label: str | None = None
    issued_at: date | None = None
    published_at: date | None = None
    effective_from: date | None = None
    instrument_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    is_test_data: bool = False
    # The document is its own instrument even when the candidate names a target (a repeal notice).
    standalone: bool = False


@dataclass
class IngestResult:
    version_id: uuid.UUID
    instrument_id: uuid.UUID
    candidate_id: uuid.UUID | None
    duplicate: bool
    article_count: int
    warnings: list[str] = field(default_factory=list)
    metadata_gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "version_id": str(self.version_id),
            "instrument_id": str(self.instrument_id),
            "candidate_id": str(self.candidate_id) if self.candidate_id else None,
            "duplicate": self.duplicate,
            "article_count": self.article_count,
            "warnings": self.warnings,
            "metadata_gaps": self.metadata_gaps,
        }


def validate_source_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise IngestionError("source_url must be an absolute http(s) URL")
    return url.strip()


def _metadata_gaps(version: DocumentVersion, instrument: LegalInstrument, settings: Settings) -> list[str]:
    """Critical metadata that is missing is flagged for the reviewer, never inferred (FR-04)."""
    gaps = []
    if not version.source_url:
        gaps.append("source_url")
    elif not version.is_test_data and (urlsplit(version.source_url).hostname or "").lower() not in settings.allowlisted_hosts:
        gaps.append("source_url_not_allowlisted")
    if not (version.source_authority or instrument.issuing_authority):
        gaps.append("issuing_authority")
    if version.published_at is None:
        gaps.append("published_at")
    if version.effective_from is None:
        gaps.append("effective_from")
    return gaps


def _resolve_instrument(
    session: Session, meta: DocumentMetadata, parsed_title: str | None, candidate: UpdateCandidate | None
) -> LegalInstrument:
    instrument_id = meta.instrument_id or (candidate.target_instrument_id if candidate and not meta.standalone else None)
    if instrument_id:
        instrument = session.get(LegalInstrument, instrument_id)
        if instrument is None:
            raise IngestionError("instrument_id does not exist")
        return instrument
    title = (meta.title_ar or parsed_title or "").strip()
    if not title:
        raise IngestionError("a document title is required (title_ar) when it cannot be read from the text")
    key = normalize_ar(title)
    instrument = session.scalars(select(LegalInstrument).where(LegalInstrument.title_normalized == key)).first()
    if instrument:
        return instrument
    instrument = LegalInstrument(
        official_title_ar=title,
        title_normalized=key,
        domain=meta.domain or "other",
        issuing_authority=meta.issuing_authority,
        instrument_type=meta.instrument_type or "law",
        canonical_source_url=meta.source_url,
        current_review_status="needs_review",
        is_test_data=meta.is_test_data,
    )
    session.add(instrument)
    session.flush()
    return instrument


def ingest_document(
    session: Session,
    *,
    content: bytes,
    filename: str | None,
    mime_type: str | None,
    meta: DocumentMetadata,
    settings: Settings,
    actor_id: uuid.UUID | None = None,
    storage_path: str | None = None,
    snapshot_id: uuid.UUID | None = None,
) -> IngestResult:
    if not content:
        raise IngestionError("empty document")
    if len(content) > settings.max_upload_bytes:
        raise IngestionError("document exceeds the maximum upload size")
    if meta.domain and meta.domain not in DOMAINS:
        raise IngestionError(f"domain must be one of {DOMAINS}")
    if meta.instrument_type and meta.instrument_type not in INSTRUMENT_TYPES:
        raise IngestionError(f"instrument_type must be one of {INSTRUMENT_TYPES}")
    source_url = validate_source_url(meta.source_url)

    kind = sniff_kind(content, filename, mime_type)
    parsed = (parse_boe_html(content) or parse_uqn_html(content)) if kind == "html" else None
    if parsed is not None:
        text = document_text(parsed)
    else:
        text = extract_text(content, kind)
        parsed = parse_legal_text(text)
    raw_hash = hashlib.sha256(content).hexdigest()

    candidate = session.get(UpdateCandidate, meta.candidate_id) if meta.candidate_id else None
    if meta.candidate_id and candidate is None:
        raise IngestionError("candidate_id does not exist")
    if candidate and candidate.state in ("approved", "rejected"):
        raise IngestionError(f"candidate is already {candidate.state}")

    instrument = _resolve_instrument(session, meta, parsed.title, candidate)

    # FR-05: the same bytes for the same instrument never create a second version.
    existing = session.scalars(
        select(DocumentVersion).where(
            DocumentVersion.instrument_id == instrument.id, DocumentVersion.raw_hash == raw_hash
        )
    ).first()
    if existing:
        linked = session.scalars(
            select(UpdateCandidate).where(UpdateCandidate.proposed_version_id == existing.id)
        ).first()
        return IngestResult(
            version_id=existing.id,
            instrument_id=instrument.id,
            candidate_id=linked.id if linked else None,
            duplicate=True,
            article_count=existing.article_count,
            warnings=["duplicate_document"],
            metadata_gaps=list(existing.metadata_gaps),
        )

    version = DocumentVersion(
        instrument_id=instrument.id,
        source_url=source_url or (candidate.canonical_url if candidate else None),
        source_authority=meta.issuing_authority,
        raw_hash=raw_hash,
        version_label=meta.version_label,
        issued_at=meta.issued_at,
        published_at=meta.published_at,
        effective_from=meta.effective_from,
        review_status="needs_review",
        index_active=False,
        original_filename=(filename or "")[:255] or None,
        mime_type={"pdf": "application/pdf", "html": "text/html", "text": "text/plain"}[kind],
        storage_path=storage_path,
        snapshot_id=snapshot_id,
        raw_text=text,
        article_count=len(parsed.articles),
        parse_warnings=parsed.warnings,
        is_test_data=meta.is_test_data or instrument.is_test_data,
        created_by=actor_id,
    )
    version.articles = [
        Article(
            article_number=a.article_number,
            article_label=a.article_label,
            chapter=a.chapter,
            original_text_ar=a.text,
            normalized_text_ar=normalize_ar(a.text),
            ordering=a.ordering,
        )
        for a in parsed.articles
    ]
    session.add(version)
    session.flush()
    version.metadata_gaps = _metadata_gaps(version, instrument, settings)

    has_base = base_version_for(session, instrument.id, exclude_id=version.id) is not None
    if candidate is None:
        if instrument.instrument_type in ("notice", "draft", "guidance"):
            classification = "notice"
        elif has_base:
            classification = "amendment"
        elif instrument.instrument_type in ("regulation", "implementing_regulation"):
            classification = "regulation"
        else:
            classification = "new_law"
        candidate = UpdateCandidate(
            origin="manual_upload",
            external_id=f"upload:{raw_hash}",
            canonical_url=version.source_url,
            title=instrument.official_title_ar,
            changed_hash=raw_hash,
            classification=classification,
            is_test_data=version.is_test_data,
        )
        session.add(candidate)
    if not meta.standalone:
        candidate.target_instrument_id = instrument.id
    candidate.proposed_version_id = version.id
    candidate.state = "needs_review"
    candidate.error = None
    candidate.diff = diff_against_base(session, version)
    session.flush()

    return IngestResult(
        version_id=version.id,
        instrument_id=instrument.id,
        candidate_id=candidate.id,
        duplicate=False,
        article_count=len(parsed.articles),
        warnings=list(parsed.warnings),
        metadata_gaps=list(version.metadata_gaps),
    )
