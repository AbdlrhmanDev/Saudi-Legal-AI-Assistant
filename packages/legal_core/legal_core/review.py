"""Operator escape hatch (PRD v2 8.2) and the shared publication primitives.

Routine updates are published by the nightly release gate (legal_core.sync) with no human step.
These functions let an operator correct, approve or reject a quarantined candidate, roll a version
back, or roll the whole index back to an earlier generation.

Every decision appends a ReviewEvent. Version text is immutable; only validity metadata (effective
interval, index activation) changes, each change is audited, and each change becomes a new
validated index generation.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.generations import (
    IndexHealthError,
    activate_generation,
    active_generation,
    apply_manifest,
    previous_generation,
)
from legal_core.indexing import index_version
from legal_core.models import (
    CLASSIFICATIONS,
    DocumentVersion,
    IndexGeneration,
    LegalInstrument,
    QuarantineEvent,
    ReviewEvent,
    UpdateCandidate,
    utcnow,
)

OPEN_STATES = ("candidate", "retrieved", "parsed", "needs_review", "failed", "quarantined")
APPROVABLE_STATES = ("needs_review", "quarantined")
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


def authoritative_versions(session: Session, instrument_id: uuid.UUID, exclude: uuid.UUID) -> list[DocumentVersion]:
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


def place_version(session: Session, version: DocumentVersion, effective: date) -> dict:
    """Make `version` authoritative from `effective`: the version in force at that date ends there
    (superseded) and the new one ends where a later scheduled version begins. Returns the before /
    after pointers for the audit trail. Raises ReviewError on a same-day conflict."""
    others = authoritative_versions(session, version.instrument_id, exclude=version.id)
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
    pointers: dict = {}
    if predecessor:
        pointers["predecessor"] = {
            "version_id": str(predecessor.id),
            "previous_effective_to": predecessor.effective_to.isoformat() if predecessor.effective_to else None,
            "previous_end_reason": predecessor.end_reason,
        }
        predecessor.effective_to, predecessor.end_reason = effective, "superseded"
    version.effective_from = effective
    version.effective_to = successor.effective_from if successor else None
    version.end_reason = "superseded" if successor else None
    version.metadata_gaps = [
        g for g in version.metadata_gaps if g != "effective_from" and not (g == "published_at" and version.published_at)
    ]
    version.review_status, version.index_active = "approved", True
    version.instrument.current_review_status = "approved"
    session.flush()
    return pointers


def resolve_quarantine(session: Session, candidate_id: uuid.UUID, actor_id: uuid.UUID | None, state: str = "resolved") -> None:
    for event in session.scalars(
        select(QuarantineEvent).where(QuarantineEvent.candidate_id == candidate_id, QuarantineEvent.resolution_state == "open")
    ):
        event.resolution_state, event.resolved_at, event.resolved_by = state, utcnow(), actor_id


def _activate(session: Session, *, trigger: str, changed: list[DocumentVersion], **kwargs) -> IndexGeneration:
    try:
        return activate_generation(session, trigger=trigger, changed=changed, **kwargs)
    except IndexHealthError as exc:
        raise ReviewError("INDEX_HEALTH_FAILED", str(exc)) from None


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


def _set_target(session: Session, candidate: UpdateCandidate, target_instrument_id: uuid.UUID) -> None:
    instrument = session.get(LegalInstrument, target_instrument_id)
    if instrument is None:
        raise ReviewError("INVALID_TARGET", "target instrument does not exist", 422)
    candidate.target_instrument_id = instrument.id
    version = candidate.proposed_version
    if version and version.instrument_id != instrument.id and candidate.classification != "repeal":
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
    if candidate.state not in APPROVABLE_STATES:
        raise ReviewError(
            "INVALID_STATE", f"only candidates in needs_review or quarantined can be approved (state: {candidate.state})"
        )
    if candidate.classification == "irrelevant":
        raise ReviewError("INVALID_STATE", "candidate is classified as irrelevant; reclassify it before approving")
    version = candidate.proposed_version
    if version is None:
        raise ReviewError("NO_DOCUMENT", "no official text is attached; upload the document for this candidate first")
    if version.review_status not in APPROVABLE_STATES:
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
    if published_at:
        version.published_at = published_at

    details: dict = {
        "classification": candidate.classification,
        "diff_summary": (candidate.diff or {}).get("summary"),
        "acknowledged_warnings": warnings,
        **place_version(session, version, effective),
    }
    version.validation_evidence = {"method": "reviewer", "effective_date_evidence": effective_date_evidence.strip()}

    # FR-18: build the search index for the approved version inside the same transaction, so a
    # failure (e.g. embedding provider down) publishes nothing.
    stats = index_version(session, version, embedder, settings)
    details["indexed_chunks"] = stats.chunks
    details["embedding_model"] = embedder.model_name
    generation = _activate(session, trigger="manual_approval", changed=[version], stats=stats)
    details["index_generation"] = generation.number

    candidate.state = "approved"
    resolve_quarantine(session, candidate.id, actor_id)
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
    if version and version.review_status in APPROVABLE_STATES:
        version.review_status = "rejected"
    resolve_quarantine(session, candidate.id, actor_id)
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
    for other in authoritative_versions(session, version.instrument_id, exclude=version.id):
        if other.end_reason == "superseded" and other.effective_to == version.effective_from:
            restored.append({"version_id": str(other.id), "previous_effective_to": other.effective_to.isoformat()})
            other.effective_to = version.effective_to
            other.end_reason = "superseded" if version.effective_to else None

    version.review_status, version.index_active = "rolled_back", False
    session.flush()
    remaining = authoritative_versions(session, version.instrument_id, exclude=version.id)
    if not remaining:
        version.instrument.current_review_status = "needs_review"
    generation = _activate(session, trigger="version_rollback", changed=[], note=reason.strip())
    event = ReviewEvent(
        version_id=version.id,
        actor_id=actor_id,
        decision="rolled_back",
        reason=reason.strip(),
        details={"restored_predecessors": restored, "index_generation": generation.number},
    )
    session.add(event)
    session.flush()
    return event


def rollback_generation(
    session: Session, *, actor_id: uuid.UUID | None, reason: str, target_generation_id: uuid.UUID | None = None
) -> IndexGeneration:
    """Restore an earlier validated generation (default: the one before the active generation).
    The rollback itself becomes a new generation, so it can be undone the same way."""
    if not reason.strip():
        raise ReviewError("REASON_REQUIRED", "a rollback reason is required", 422)
    current = active_generation(session)
    if current is None:
        raise ReviewError("NO_ACTIVE_GENERATION", "there is no active index generation to roll back")
    if target_generation_id is None:
        target = previous_generation(session, current)
    else:
        target = session.get(IndexGeneration, target_generation_id)
        if target is not None and target.validation_status not in ("retired", "active"):
            raise ReviewError("INVALID_TARGET", "only a previously validated generation can be restored", 422)
    if target is None:
        raise ReviewError("NO_PREVIOUS_GENERATION", "there is no earlier validated generation to restore")
    if target.id == current.id:
        raise ReviewError("INVALID_TARGET", "the target generation is already active", 422)
    try:
        touched = apply_manifest(session, target.manifest)
    except LookupError as exc:
        raise ReviewError("MANIFEST_UNAVAILABLE", str(exc)) from None
    note = f"rollback to generation {target.number}: {reason.strip()}"
    generation = _activate(session, trigger="rollback", changed=[], note=note, parent_id=target.id)
    session.add(
        ReviewEvent(
            actor_id=actor_id,
            decision="generation_rolled_back",
            reason=reason.strip(),
            details={
                "from_generation": current.number,
                "to_generation": target.number,
                "new_generation": generation.number,
                "versions_changed": [str(v.id) for v in touched],
            },
        )
    )
    session.flush()
    return generation
