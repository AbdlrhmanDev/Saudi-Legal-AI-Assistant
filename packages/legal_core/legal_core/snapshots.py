"""Immutable source snapshots (PRD v2 FR-04): the exact bytes every version was parsed from."""

import hashlib
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from legal_core.models import SourceSnapshot, utcnow


def store_snapshot(
    session: Session,
    content: bytes,
    *,
    canonical_url: str | None,
    source_id: uuid.UUID | None = None,
    content_type: str | None = None,
    published_at: date | None = None,
    is_test_data: bool = False,
) -> SourceSnapshot:
    """Get-or-create by (source, checksum): refetching unchanged bytes never adds a row."""
    checksum = hashlib.sha256(content).hexdigest()
    source_key = canonical_url or "upload"
    session.execute(
        insert(SourceSnapshot)
        .values(
            id=uuid.uuid4(),
            source_id=source_id,
            canonical_url=canonical_url,
            source_key=source_key,
            body_checksum=checksum,
            content=content,
            content_type=content_type,
            byte_size=len(content),
            retrieved_at=utcnow(),
            published_at=published_at,
            is_test_data=is_test_data,
        )
        .on_conflict_do_nothing(constraint="uq_snapshots_source_checksum")
    )
    return session.scalars(
        select(SourceSnapshot).where(SourceSnapshot.source_key == source_key, SourceSnapshot.body_checksum == checksum)
    ).one()


def snapshot_content(session: Session, snapshot_id: uuid.UUID) -> bytes:
    content = session.scalar(select(SourceSnapshot.content).where(SourceSnapshot.id == snapshot_id))
    if content is None:
        raise LookupError(f"snapshot {snapshot_id} not found")
    return content
