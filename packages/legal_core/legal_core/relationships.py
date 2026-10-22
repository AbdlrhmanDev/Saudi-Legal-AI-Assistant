"""Deterministic links between legal instruments (document_relationships).

* implementing_regulation_of: "اللائحة التنفيذية لنظام X" -> the instrument titled "نظام X".
* repeals: a published repeal notice -> the instrument it ended (from amendment_relations).

Links are derived only from official titles and published amendment relations, never guessed from
content similarity, and inserting the same link twice is a no-op.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from legal_core.models import AmendmentRelation, DocumentRelationship, DocumentVersion, LegalInstrument
from legal_core.normalize import normalize_ar

_PARENS = re.compile(r"[\(\[].*?[\)\]]")
_IMPLEMENTING = re.compile(r"^(?:ال)?لايحه\s+(?:ال)?تنفيذيه\s+(?:ل|ال)?(?P<parent>(?:نظام|لنظام)\s+.+)$")


def _key(title: str) -> str:
    return " ".join(normalize_ar(_PARENS.sub(" ", title)).split())


def _link(session: Session, source: uuid.UUID, target: uuid.UUID, kind: str, evidence: str) -> int:
    result = session.execute(
        insert(DocumentRelationship)
        .values(id=uuid.uuid4(), from_instrument_id=source, to_instrument_id=target, relation_type=kind, evidence=evidence)
        .on_conflict_do_nothing(constraint="uq_document_relationship")
        .returning(DocumentRelationship.id)
    )
    return len(result.all())


def link_instruments(session: Session) -> int:
    """Create missing links; returns how many were added."""
    instruments = session.execute(select(LegalInstrument.id, LegalInstrument.official_title_ar)).all()
    by_title = {_key(title): instrument_id for instrument_id, title in instruments}
    added = 0
    for instrument_id, title in instruments:
        match = _IMPLEMENTING.match(_key(title))
        if not match:
            continue
        parent_title = match.group("parent").removeprefix("ل")
        parent = by_title.get(parent_title)
        if parent and parent != instrument_id:
            added += _link(session, instrument_id, parent, "implementing_regulation_of", f"title: {title}")
    repeals = session.execute(
        select(DocumentVersion.instrument_id, AmendmentRelation.target_instrument_id, AmendmentRelation.publication_evidence)
        .join(DocumentVersion, AmendmentRelation.amending_version_id == DocumentVersion.id)
        .where(AmendmentRelation.change_type == "repealed")
    ).all()
    for notice, target, evidence in repeals:
        if notice != target:
            added += _link(session, notice, target, "repeals", evidence or "published repeal")
    session.flush()
    return added
