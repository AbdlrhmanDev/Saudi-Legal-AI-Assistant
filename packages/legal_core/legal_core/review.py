"""Review-before-publish (FR-14..FR-18). Nothing becomes authoritative outside this module.

Every decision appends a ReviewEvent. Version text is immutable; only validity
metadata (effective interval, index activation) changes, and each change is audited.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.indexing import reindex_version
from legal_core.models import (
    CLASSIFICATIONS,
    DocumentVersion,
    LegalInstrument,
    ReviewEvent,
    UpdateCandidate,
)

OPEN_STATES = ("candidate", "retrieved", "parsed", "needs_review", "failed")
# Warnings a reviewer must explicitly acknowledge before content can become authoritative.
BLOCKING_WARNINGS = ("possible_prompt_injection", "possible_partial_amendment_document", "no_articles_detected")


class ReviewError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _candidate(session: Session, candidate_id: uuid.UUID) -> UpdateCandidate:
    candidate = session.get(UpdateCandidate, candidate_id, with_for_update=True)
    if candidate is None:
        raise ReviewError("NOT_FOUND", "update candidate not found", 404)
    return candidate


def _authoritative_versions(session: Session, instrument_id: uuid.UUID, exclude: uuid.UUID) -> list[DocumentVersion]:
    return list(
        session.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.instrument_id == instrument_id,
                DocumentVersion.review_status == "approved",
                DocumentVersion.index_active.is_(True),
                DocumentVersion.id != exclude,
            )
            .order_by(DocumentVersion.effective_from)
            .with_for_update()
        )
    )


def _set_target(session: Session, candidate: UpdateCandidate, target_instrument_id: uuid.UUID) -> None:
    instrument = session.get(LegalInstrument, target_instrument_id)
    if instrument is None:
        raise ReviewError("INVALID_TARGET", "target instrument does not exist", 422)
    candidate.target_instrument_id = instrument.id
    version = candidate.proposed_version
    if version and version.instrument_id != instrument.id:
        clash = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.instrument_id == instrument.id, DocumentVersion.raw_hash == version.raw_hash
            )
        ).first()
        if clash:
            raise ReviewError("DUPLICATE_VERSION", "the target instrument already has this exact document")
        version.instrument_id = instrument.id
        session.flush()
        session.refresh(version, ["instrument"])


def update_candidate(
    session: Session,
    candidate_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    classification: str | None = None,
    target_instrument_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> UpdateCandidate:
    """Reviewer override of the automatic classification / affected instrument (FR-14)."""
    from legal_core.diffing import diff_against_base

    candidate = _candidate(session, candidate_id)
    if candidate.state not in OPEN_STATES:
        raise ReviewError("INVALID_STATE", f"candidate is already {candidate.state}")
    details: dict = {}
    if classification is not None:
        if classification not in CLASSIFICATIONS:
            raise ReviewError("INVALID_CLASSIFICATION", f"classification must be one of {CLASSIFICATIONS}", 422)
        details["classification"] = {"from": candidate.classification, "to": classification}
        candidate.classification, candidate.classification_source = classification, "reviewer"
    if target_instrument_id is not None:
        details["target_instrument_id"] = {
            "from": str(candidate.target_instrument_id) if candidate.target_instrument_id else None,
            "to": str(target_instrument_id),
        }
        _set_target(session, candidate, target_instrument_id)
        if candidate.proposed_version:
            candidate.diff = diff_against_base(session, candidate.proposed_version)
    session.add(
        ReviewEvent(
            candidate_id=candidate.id,
            version_id=candidate.proposed_version_id,
            actor_id=actor_id,
            decision="classification_override",
            reason=reason,
            details=details,
        )
    )
    session.flush()
    return candidate


def approve_candidate(
    session: Session,
    candidate_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    effective_date_evidence: str,
    embedder: Embedder,
    settings: Settings,
    effective_from: date | None = None,
    published_at: date | None = None,
    reason: str | None = None,
    acknowledge_warnings: bool = False,
) -> ReviewEvent:
    candidate = _candidate(session, candidate_id)
    if candidate.state != "needs_review":
        raise ReviewError("INVALID_STATE", f"only candidates in needs_review can be approved (state: {candidate.state})")
    if candidate.classification == "irrelevant":
        raise ReviewError("INVALID_STATE", "candidate is classified as irrelevant; reclassify it before approving")
    version = candidate.proposed_version
    if version is None:
        raise ReviewError("NO_DOCUMENT", "no official text is attached; upload the document for this candidate first")
    if version.review_status != "needs_review":
        raise ReviewError("INVALID_STATE", f"proposed version is {version.review_status}")
    if not effective_date_evidence.strip():
        raise ReviewError("EVIDENCE_REQUIRED", "effective_date_evidence is required", 422)

    warnings = [
        w for w in [*version.parse_warnings, *(candidate.diff or {}).get("warnings", [])] if w in BLOCKING_WARNINGS
    ]
    if warnings and not acknowledge_warnings:
        raise ReviewError(
            "WARNINGS_NOT_ACKNOWLEDGED",
            f"review these warnings and resubmit with acknowledge_warnings=true: {', '.join(warnings)}",
        )

    effective = effective_from or version.effective_from
    if effective is None:
        # FR-04 / 8.3: an unknown effective date blocks approval; publication date is never substituted.
        raise ReviewError(
            "EFFECTIVE_DATE_REQUIRED", "effective_from must be confirmed by the reviewer before approval", 422
        )

    others = _authoritative_versions(session, version.instrument_id, exclude=version.id)
    if any(o.effective_from == effective for o in others):
        raise ReviewError(
            "CONFLICTING_VERSION",
            "another approved version of this instrument starts on the same effective date; roll it back or adjudicate",
        )
    predecessor = next(
        (o for o in reversed(others) if o.effective_from < effective and (o.effective_to is None or o.effective_to > effective)),
        None,
    )
    successor = next((o for o in others if o.effective_from > effective), None)
    details: dict = {
        "classification": candidate.classification,
        "diff_summary": (candidate.diff or {}).get("summary"),
        "acknowledged_warnings": warnings,
    }
    if predecessor:
        details["predecessor"] = {
            "version_id": str(predecessor.id),
            "previous_effective_to": predecessor.effective_to.isoformat() if predecessor.effective_to else None,
            "previous_end_reason": predecessor.end_reason,
        }
        predecessor.effective_to, predecessor.end_reason = effective, "superseded"

    version.effective_from = effective
    version.effective_to = successor.effective_from if successor else None
    version.end_reason = "superseded" if successor else None
    if published_at:
        version.published_at = published_at
    version.metadata_gaps = [
        g for g in version.metadata_gaps if g != "effective_from" and not (g == "published_at" and version.published_at)
    ]
    version.review_status, version.index_active = "approved", True
    session.flush()

    # FR-18: build the search index for the approved version inside the same transaction, so a
    # failure (e.g. embedding provider down) publishes nothing.
    details["indexed_chunks"] = reindex_version(session, version, embedder, settings)
    details["embedding_model"] = embedder.model_name

    version.instrument.current_review_status = "approved"
    candidate.state = "approved"
    event = ReviewEvent(
        candidate_id=candidate.id,
        version_id=version.id,
        actor_id=actor_id,
        decision="approved",
        reason=reason,
        effective_date_evidence=effective_date_evidence.strip(),
        details=details,
    )
    session.add(event)
    session.flush()
    return event


def reject_candidate(
    session: Session, candidate_id: uuid.UUID, *, actor_id: uuid.UUID | None, reason: str
) -> ReviewEvent:
    candidate = _candidate(session, candidate_id)
    if candidate.state not in OPEN_STATES:
        raise ReviewError("INVALID_STATE", f"candidate is already {candidate.state}")
    if not reason.strip():
        raise ReviewError("REASON_REQUIRED", "a rejection reason is required", 422)
    candidate.state = "rejected"
    version = candidate.proposed_version
    if version and version.review_status == "needs_review":
        version.review_status = "rejected"
    event = ReviewEvent(
        candidate_id=candidate.id,
        version_id=candidate.proposed_version_id,
        actor_id=actor_id,
        decision="rejected",
        reason=reason.strip(),
    )
    session.add(event)
    session.flush()
    return event


def rollback_version(
    session: Session, version_id: uuid.UUID, *, actor_id: uuid.UUID | None, reason: str
) -> ReviewEvent:
    """Deactivate an approved version in the index and restore its predecessor's validity.

    Nothing is deleted: the version, its articles and all prior events stay for audit.
    """
    version = session.get(DocumentVersion, version_id, with_for_update=True)
    if version is None:
        raise ReviewError("NOT_FOUND", "version not found", 404)
    if version.review_status != "approved" or not version.index_active:
        raise ReviewError("INVALID_STATE", "only an approved, active version can be rolled back")
    if not reason.strip():
        raise ReviewError("REASON_REQUIRED", "a rollback reason is required", 422)

    restored = []
    for other in _authoritative_versions(session, version.instrument_id, exclude=version.id):
        if other.end_reason == "superseded" and other.effective_to == version.effective_from:
            restored.append({"version_id": str(other.id), "previous_effective_to": other.effective_to.isoformat()})
            other.effective_to = version.effective_to
            other.end_reason = "superseded" if version.effective_to else None

    version.review_status, version.index_active = "rolled_back", False
    session.flush()
    remaining = _authoritative_versions(session, version.instrument_id, exclude=version.id)
    if not remaining:
        version.instrument.current_review_status = "needs_review"
    event = ReviewEvent(
        version_id=version.id,
        actor_id=actor_id,
        decision="rolled_back",
        reason=reason.strip(),
        details={"restored_predecessors": restored},
    )
    session.add(event)
    session.flush()
    return event
