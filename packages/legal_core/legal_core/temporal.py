"""Legal effective-time rules (PRD section 8).

"Approved" means the ingestion was verified. Whether a rule is in force is a
function of the as-of date: scheduled -> active -> superseded/repealed.
Publication date is never used as a substitute for the effective date.
"""

from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from legal_core.models import DocumentVersion


def legal_status(version: DocumentVersion, as_of: date) -> str:
    if version.review_status != "approved":
        return version.review_status
    if not version.index_active:
        return "rolled_back"
    if version.effective_from is None:
        return "needs_review"
    if as_of < version.effective_from:
        return "scheduled"
    if version.effective_to is not None and as_of >= version.effective_to:
        return version.end_reason or "superseded"
    return "active"


def _authoritative() -> ColumnElement[bool]:
    return and_(DocumentVersion.review_status == "approved", DocumentVersion.index_active.is_(True))


def in_force_on(as_of: date) -> ColumnElement[bool]:
    """Reviewed versions legally effective on `as_of` (default current-law retrieval)."""
    return and_(
        _authoritative(),
        DocumentVersion.effective_from <= as_of,
        or_(DocumentVersion.effective_to.is_(None), DocumentVersion.effective_to > as_of),
    )


def forthcoming_after(as_of: date) -> ColumnElement[bool]:
    """Reviewed versions published/approved but not yet effective on `as_of`."""
    return and_(_authoritative(), DocumentVersion.effective_from > as_of)
