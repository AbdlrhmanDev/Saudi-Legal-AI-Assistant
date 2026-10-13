"""Article-level comparison between a proposed version and the version it would replace (FR-15)."""

import difflib
import re
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from legal_core.models import Article, DocumentVersion

_WS = re.compile(r"\s+")


def _article_key(article: Article) -> str:
    return article.article_number or f"unnumbered:{article.article_label or article.ordering}"


def _canonical(text: str) -> str:
    return _WS.sub(" ", text).strip()


def base_version_for(
    session: Session, instrument_id: uuid.UUID, exclude_id: uuid.UUID | None = None, before: date | None = None
) -> DocumentVersion | None:
    """Latest authoritative version of the instrument (optionally effective before a date)."""
    stmt = select(DocumentVersion).where(
        DocumentVersion.instrument_id == instrument_id,
        DocumentVersion.review_status == "approved",
        DocumentVersion.index_active.is_(True),
    )
    if exclude_id:
        stmt = stmt.where(DocumentVersion.id != exclude_id)
    if before:
        stmt = stmt.where(DocumentVersion.effective_from < before)
    return session.scalars(stmt.order_by(DocumentVersion.effective_from.desc())).first()


def diff_articles(old: list[Article], new: list[Article]) -> dict:
    old_by_key = {_article_key(a): a for a in old}
    new_by_key = {_article_key(a): a for a in new}
    changes: list[dict] = []
    unchanged = 0

    for key, article in new_by_key.items():
        before = old_by_key.get(key)
        if before is None:
            changes.append(_change(key, "added", None, article))
        elif _canonical(before.original_text_ar) != _canonical(article.original_text_ar):
            changes.append(_change(key, "changed", before, article))
        else:
            unchanged += 1
    for key, article in old_by_key.items():
        if key not in new_by_key:
            changes.append(_change(key, "removed", article, None))

    counts = {kind: sum(1 for c in changes if c["change"] == kind) for kind in ("added", "changed", "removed")}
    warnings = []
    if old and counts["removed"] > len(old) / 2:
        # A document holding only the amended articles looks like a mass repeal when compared
        # as a consolidated text. The reviewer must upload the consolidated version instead.
        warnings.append("possible_partial_amendment_document")
    return {
        "summary": {**counts, "unchanged": unchanged},
        "affected_article_numbers": [c["article_number"] for c in changes],
        "changes": changes,
        "warnings": warnings,
    }


def _change(key: str, kind: str, before: Article | None, after: Article | None) -> dict:
    before_text = before.original_text_ar if before else ""
    after_text = after.original_text_ar if after else ""
    snippet = list(
        difflib.unified_diff(before_text.splitlines(), after_text.splitlines(), "before", "after", lineterm="", n=1)
    )
    return {
        "article_number": key,
        "change": kind,
        "before_article_id": str(before.id) if before else None,
        "after_article_id": str(after.id) if after else None,
        "before": before_text or None,
        "after": after_text or None,
        "snippet": snippet[2:],
    }


def diff_against_base(session: Session, version: DocumentVersion) -> dict:
    base = base_version_for(session, version.instrument_id, exclude_id=version.id)
    if base is None:
        return {
            "base_version_id": None,
            "summary": {"added": len(version.articles), "changed": 0, "removed": 0, "unchanged": 0},
            "affected_article_numbers": [],
            "changes": [],
            "warnings": ["no_base_version"],
        }
    return {"base_version_id": str(base.id), **diff_articles(base.articles, version.articles)}
