"""Public, read-only catalog. Only reviewed (approved + index-active) material is exposed."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import exists, func, or_, select

from api import serializers
from api.deps import ApiError, Db, PageDep, SettingsDep, ok
from api.schemas import Domain
from legal_core.answer import today_riyadh
from legal_core.freshness import cached_freshness
from legal_core.models import AmendmentRelation, Article, DocumentVersion, LegalInstrument
from legal_core.normalize import normalize_ar
from legal_core.temporal import in_force_on

router = APIRouter(prefix="/api/v1", tags=["laws"])

_REVIEWED = (DocumentVersion.review_status == "approved") & DocumentVersion.index_active.is_(True)


def _reviewed_instrument(db: Db, law_id: uuid.UUID) -> LegalInstrument:
    instrument = db.scalars(
        select(LegalInstrument).where(
            LegalInstrument.id == law_id,
            exists().where(DocumentVersion.instrument_id == LegalInstrument.id, _REVIEWED),
        )
    ).first()
    if instrument is None:
        raise ApiError(404, "NOT_FOUND", "Legal instrument not found.")
    return instrument


@router.get("/laws", summary="Search and filter the legal instrument catalog")
def list_laws(
    db: Db,
    page: PageDep,
    q: Annotated[str | None, Query(max_length=200, description="Arabic title search (typo-tolerant).")] = None,
    domain: Domain | None = None,
    instrument_type: Annotated[str | None, Query(max_length=40)] = None,
) -> dict:
    stmt = select(LegalInstrument).where(
        exists().where(DocumentVersion.instrument_id == LegalInstrument.id, _REVIEWED)
    )
    if domain:
        stmt = stmt.where(LegalInstrument.domain == domain)
    if instrument_type:
        stmt = stmt.where(LegalInstrument.instrument_type == instrument_type)
    term = normalize_ar(q) if q else ""
    if term:
        similarity = func.word_similarity(term, LegalInstrument.title_normalized)
        stmt = stmt.where(or_(LegalInstrument.title_normalized.contains(term, autoescape=True), similarity >= 0.6))
        stmt = stmt.order_by(similarity.desc(), LegalInstrument.official_title_ar)
    else:
        stmt = stmt.order_by(LegalInstrument.domain, LegalInstrument.official_title_ar)
    return ok(page.run(db, stmt, serializers.instrument))


@router.get("/coverage", summary="What the assistant covers (shown to users to avoid over-reliance)")
def coverage(db: Db) -> dict:
    today = today_riyadh()
    rows = db.execute(
        select(LegalInstrument.domain, func.count(func.distinct(LegalInstrument.id)), func.count(Article.id))
        .join(DocumentVersion, DocumentVersion.instrument_id == LegalInstrument.id)
        .join(Article, Article.version_id == DocumentVersion.id)
        .where(in_force_on(today))
        .group_by(LegalInstrument.domain)
    ).all()
    return ok(
        {
            "as_of_date": today.isoformat(),
            "domains": [{"domain": d, "instruments": i, "articles": a} for d, i, a in rows],
            "notice_ar": "يغطي المساعد مجموعة محدودة ومختارة من الأنظمة، ولا يمثل تغطية كاملة للأنظمة السعودية.",
        }
    )


@router.get("/laws/{law_id}", summary="One legal instrument")
def get_law(law_id: uuid.UUID, db: Db) -> dict:
    return ok(serializers.instrument(_reviewed_instrument(db, law_id)))


@router.get("/laws/{law_id}/versions", summary="Reviewed versions with effective dates and status")
def list_versions(law_id: uuid.UUID, db: Db, as_of_date: date | None = None) -> dict:
    instrument = _reviewed_instrument(db, law_id)
    as_of = as_of_date or today_riyadh()
    versions = db.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.instrument_id == instrument.id, _REVIEWED)
        .order_by(DocumentVersion.effective_from.desc())
    ).all()
    return ok({"law": serializers.instrument(instrument), "versions": [serializers.version(v, as_of) for v in versions]})


@router.get("/versions/{version_id}/articles", summary="Articles of a reviewed version")
def list_articles(version_id: uuid.UUID, db: Db) -> dict:
    version = db.scalars(select(DocumentVersion).where(DocumentVersion.id == version_id, _REVIEWED)).first()
    if version is None:
        raise ApiError(404, "NOT_FOUND", "Version not found.")
    return ok(
        {
            "version": serializers.version(version, today_riyadh()),
            "law": serializers.instrument(version.instrument),
            "articles": [serializers.article(a) for a in version.articles],
        }
    )


@router.get("/articles/{article_id}", summary="Supporting passage for a citation")
def get_article(article_id: uuid.UUID, db: Db) -> dict:
    row = db.execute(
        select(Article, DocumentVersion)
        .join(DocumentVersion, Article.version_id == DocumentVersion.id)
        .where(Article.id == article_id, _REVIEWED)
    ).first()
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Article not found.")
    article, version = row
    return ok(
        {
            "article": serializers.article(article),
            "version": serializers.version(version, today_riyadh()),
            "law": serializers.instrument(version.instrument),
        }
    )


@router.get(
    "/freshness",
    summary="Last verified sync, source health and coverage (shown with every answer)",
    description=(
        "`corpus_verified_at` is when the last nightly synchronization finished. It does not mean every "
        "future amendment is already included; `source_health` and `warning_ar` say when sources are stale."
    ),
)
def freshness(db: Db, settings: SettingsDep) -> dict:
    return ok(cached_freshness(db, settings))


@router.get("/updates", summary="Confirmed legal updates published by the synchronization, newest first")
def confirmed_updates(db: Db, page: PageDep, law_id: uuid.UUID | None = None, domain: Domain | None = None) -> dict:
    today = today_riyadh()
    stmt = (
        select(AmendmentRelation, LegalInstrument, DocumentVersion)
        .join(LegalInstrument, AmendmentRelation.target_instrument_id == LegalInstrument.id)
        .join(DocumentVersion, AmendmentRelation.amending_version_id == DocumentVersion.id)
        .where(
            or_(
                _REVIEWED,
                DocumentVersion.review_status == "evidence",
            )
        )
        .order_by(AmendmentRelation.created_at.desc(), AmendmentRelation.id)
    )
    if law_id:
        stmt = stmt.where(LegalInstrument.id == law_id)
    if domain:
        stmt = stmt.where(LegalInstrument.domain == domain)
    return ok(page.run(db, stmt, lambda row: serializers.amendment(*row, today), scalars=False))
