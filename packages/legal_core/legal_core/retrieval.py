"""Hybrid retrieval (PRD 9.1): filters first, then lexical + vector ranking fused with RRF.

Review-state and effective-time filters are applied in SQL before any ranking, so
unreviewed, rolled-back, scheduled or superseded text can never be ranked for a
default question.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.indexing import article_context
from legal_core.models import Article, ArticleChunk, DocumentVersion, LegalInstrument
from legal_core.normalize import detect_language, lexical_tokens
from legal_core.parser import _DIGITS, INJECTION_PATTERNS, clean_source_text, parse_ordinal
from legal_core.temporal import in_force_on

log = logging.getLogger(__name__)

_ARTICLE_MENTION = re.compile(r"(?:ال)?ماد[ةه]\s+")
_PARENS = re.compile(r"[\(\[].*?[\)\]]")
_LEXICAL_POOL = 100


@dataclass
class RetrievedArticle:
    article_id: uuid.UUID
    version_id: uuid.UUID
    instrument_id: uuid.UUID
    law_title_ar: str
    domain: str
    article_number: str | None
    article_label: str | None
    chapter: str | None
    text: str
    best_chunk_text: str
    source_url: str | None
    effective_from: date
    effective_to: date | None
    is_test_data: bool
    rank: int = 0
    rrf_score: float = 0.0
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_similarity: float | None = None
    lexical_coverage: float = 0.0
    strong: bool = False
    quarantined: bool = False

    def trace(self) -> dict:
        return {
            "article_id": str(self.article_id),
            "version_id": str(self.version_id),
            "article_number": self.article_number,
            "rank": self.rank,
            "rrf_score": round(self.rrf_score, 6),
            "lexical_rank": self.lexical_rank,
            "lexical_score": None if self.lexical_score is None else round(self.lexical_score, 6),
            "vector_rank": self.vector_rank,
            "vector_similarity": None if self.vector_similarity is None else round(self.vector_similarity, 4),
            "lexical_coverage": round(self.lexical_coverage, 3),
            "strong": self.strong,
            "quarantined": self.quarantined,
        }


@dataclass
class RetrievalResult:
    items: list[RetrievedArticle] = field(default_factory=list)
    language: str = "unknown"
    query_tokens: list[str] = field(default_factory=list)
    article_hints: list[str] = field(default_factory=list)
    instrument_hints: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)


def extract_article_hints(query: str) -> list[str]:
    """Article numbers the user explicitly asked about ("المادة الخامسة", "المادة 5")."""
    hints: list[str] = []
    for m in _ARTICLE_MENTION.finditer(query):
        words = query[m.end() :].translate(_DIGITS).split()[:5]
        if not words:
            continue
        digits = re.match(r"^[\(\[]?(\d{1,4})[\)\]]?", words[0])
        number = digits.group(1) if digits else None
        if number is None:
            value, _ = parse_ordinal(words)
            number = str(value) if value is not None else None
        if number and number not in hints:
            hints.append(number)
    return hints


def mentioned_article_numbers(text_: str) -> set[str]:
    return set(extract_article_hints(text_))


def rrf_fuse(rankings: list[list[uuid.UUID]], k: int) -> dict[uuid.UUID, float]:
    """Reciprocal rank fusion: score(d) = sum over lists of 1 / (k + rank)."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


def _dedupe(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def _eligible(as_of: date, domain: str | None):
    stmt = (
        select()
        .select_from(ArticleChunk)
        .join(Article, ArticleChunk.article_id == Article.id)
        .join(DocumentVersion, Article.version_id == DocumentVersion.id)
        .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(in_force_on(as_of))
    )
    if domain:
        stmt = stmt.where(LegalInstrument.domain == domain)
    return stmt


def _instrument_hints(session: Session, tokens: set[str], as_of: date, domain: str | None) -> set[uuid.UUID]:
    """Statutes named in the question: at least half of the title's content stems appear in it."""
    stmt = (
        select(LegalInstrument.id, LegalInstrument.official_title_ar)
        .join(DocumentVersion, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(in_force_on(as_of))
        .distinct()
    )
    if domain:
        stmt = stmt.where(LegalInstrument.domain == domain)
    hinted = set()
    for instrument_id, title in session.execute(stmt):
        title_tokens = set(lexical_tokens(_PARENS.sub(" ", title)))
        if title_tokens and len(title_tokens & tokens) / len(title_tokens) >= 0.5:
            hinted.add(instrument_id)
    return hinted


def retrieve(
    session: Session,
    *,
    query: str,
    as_of: date,
    domain: str | None,
    embedder: Embedder,
    settings: Settings,
    top_k: int | None = None,
) -> RetrievalResult:
    top_k = top_k or settings.retrieval_top_k
    tokens = list(dict.fromkeys(lexical_tokens(query)))
    token_set = set(tokens)
    result = RetrievalResult(
        language=detect_language(query), query_tokens=tokens, article_hints=extract_article_hints(query)
    )
    eligible = _eligible(as_of, domain)
    instrument_of: dict[uuid.UUID, uuid.UUID] = {}
    best_chunk: dict[uuid.UUID, str] = {}

    # 1) Lexical: PostgreSQL full-text over light-stemmed Arabic tokens. ts_rank has no IDF,
    #    so the pool is re-ordered by how many distinct query stems each chunk covers.
    lexical_ids: list[uuid.UUID] = []
    lexical_scores: dict[uuid.UUID, float] = {}
    if tokens:
        tsq = func.to_tsquery("simple", " | ".join(tokens))
        score = func.ts_rank(ArticleChunk.lexical_index, tsq, 1)
        rows = session.execute(
            eligible.add_columns(
                Article.id, DocumentVersion.instrument_id, ArticleChunk.chunk_text, ArticleChunk.lexical_text, score
            )
            .where(ArticleChunk.lexical_index.op("@@")(tsq))
            .order_by(score.desc(), ArticleChunk.id)
            .limit(_LEXICAL_POOL)
        ).all()
        scored = sorted(
            rows, key=lambda r: (-len(token_set & set(r.lexical_text.split())), -float(r[4]), str(r[0]))
        )
        for article_id, instrument_id, chunk_text, _lex, rank_score in scored:
            instrument_of[article_id] = instrument_id
            best_chunk.setdefault(article_id, chunk_text)
            lexical_scores.setdefault(article_id, float(rank_score))
            lexical_ids.append(article_id)
        lexical_ids = _dedupe(lexical_ids)[: settings.retrieval_candidate_k]

    # 2) Vector: pgvector cosine distance. A provider failure degrades to lexical-only.
    vector_ids: list[uuid.UUID] = []
    vector_sims: dict[uuid.UUID, float] = {}
    try:
        qvec = embedder.embed([query])[0]
    except Exception as exc:
        log.warning("embedding_failed", extra={"error": type(exc).__name__})
        result.degraded.append("vector_search_unavailable")
        qvec = None
    if qvec is not None:
        session.execute(text("SET LOCAL hnsw.ef_search = 200"))
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        distance = ArticleChunk.embedding.cosine_distance(qvec)
        rows = session.execute(
            eligible.add_columns(Article.id, DocumentVersion.instrument_id, ArticleChunk.chunk_text, distance)
            .where(ArticleChunk.embedding.is_not(None), ArticleChunk.embedding_model == embedder.model_name)
            .order_by(distance, ArticleChunk.id)
            .limit(settings.retrieval_candidate_k * 2)
        ).all()
        for article_id, instrument_id, chunk_text, dist in rows:
            instrument_of[article_id] = instrument_id
            best_chunk.setdefault(article_id, chunk_text)
            vector_sims.setdefault(article_id, 1.0 - float(dist))
            vector_ids.append(article_id)
        vector_ids = _dedupe(vector_ids)[: settings.retrieval_candidate_k]

    rankings = [lexical_ids, vector_ids]

    # 3) Query hints become extra rankings: an explicitly requested article number and
    #    a statute named in the question.
    hinted_instruments = _instrument_hints(session, token_set, as_of, domain) if tokens else set()
    result.instrument_hints = [str(i) for i in hinted_instruments]
    pool = _dedupe(lexical_ids + vector_ids)
    if hinted_instruments:
        rankings.append([a for a in pool if instrument_of[a] in hinted_instruments])
    if result.article_hints:
        stmt = (
            select(Article.id, DocumentVersion.instrument_id)
            .join(DocumentVersion, Article.version_id == DocumentVersion.id)
            .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
            .where(in_force_on(as_of), Article.article_number.in_(result.article_hints))
        )
        if domain:
            stmt = stmt.where(LegalInstrument.domain == domain)
        hinted = session.execute(stmt).all()
        if hinted_instruments:
            hinted = [h for h in hinted if h[1] in hinted_instruments] or hinted
        order = {a: i for i, a in enumerate(pool)}
        hinted.sort(key=lambda h: (order.get(h[0], len(order)), str(h[0])))
        for article_id, instrument_id in hinted:
            instrument_of[article_id] = instrument_id
        rankings.append([h[0] for h in hinted])

    fused = rrf_fuse(rankings, settings.rrf_k)
    top_ids = sorted(fused, key=lambda a: (-fused[a], str(a)))[:top_k]
    if not top_ids:
        return result

    rows = session.execute(
        select(Article, DocumentVersion, LegalInstrument)
        .join(DocumentVersion, Article.version_id == DocumentVersion.id)
        .join(LegalInstrument, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(Article.id.in_(top_ids))
    ).all()
    by_id = {article.id: (article, version, instrument) for article, version, instrument in rows}
    lexical_rank = {a: i for i, a in enumerate(lexical_ids, start=1)}
    vector_rank = {a: i for i, a in enumerate(vector_ids, start=1)}
    hinted_articles = set(rankings[-1]) if result.article_hints else set()

    for rank, article_id in enumerate(top_ids, start=1):
        article, version, instrument = by_id[article_id]
        source_url = version.source_url or instrument.canonical_source_url
        evidence_text = clean_source_text(article.original_text_ar, source_url, instrument.official_title_ar)
        chunk_text = clean_source_text(best_chunk.get(article_id, evidence_text), source_url, instrument.official_title_ar)
        # A legacy UQN chunk can consist entirely of page chrome. Use the cleaned
        # official article instead; new imports are clean before chunking.
        if source_url and "uqn.gov.sa" in source_url and len(evidence_text) > len(chunk_text):
            chunk_text = evidence_text
        haystack = set(lexical_tokens(f"{article_context(instrument.official_title_ar, article)}\n{evidence_text}"))
        coverage = len(token_set & haystack) / len(token_set) if token_set else 0.0
        similarity = vector_sims.get(article_id)
        # Source isolation: a passage that addresses the assistant ("ignore previous instructions ...")
        # is never used as evidence, whatever it scores. It stays visible in the trace.
        quarantined = bool(INJECTION_PATTERNS.search(evidence_text))
        strong = not quarantined and (
            coverage >= settings.min_lexical_coverage
            or (embedder.is_semantic and similarity is not None and similarity >= settings.min_vector_similarity)
            or (article_id in hinted_articles and bool(hinted_instruments))
        )
        result.items.append(
            RetrievedArticle(
                article_id=article.id,
                version_id=version.id,
                instrument_id=instrument.id,
                law_title_ar=instrument.official_title_ar,
                domain=instrument.domain,
                article_number=article.article_number,
                article_label=article.article_label,
                chapter=article.chapter,
                text=evidence_text,
                best_chunk_text=chunk_text,
                source_url=source_url,
                effective_from=version.effective_from,
                effective_to=version.effective_to,
                is_test_data=version.is_test_data,
                rank=rank,
                rrf_score=fused[article_id],
                lexical_rank=lexical_rank.get(article_id),
                lexical_score=lexical_scores.get(article_id),
                vector_rank=vector_rank.get(article_id),
                vector_similarity=similarity,
                lexical_coverage=coverage,
                strong=strong,
                quarantined=quarantined,
            )
        )
    return result
