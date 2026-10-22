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
from difflib import SequenceMatcher

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.indexing import article_context
from legal_core.models import Article, ArticleChunk, DocumentVersion, LegalInstrument
from legal_core.normalize import detect_language, lexical_tokens, normalize_ar, query_token_forms
from legal_core.parser import _DIGITS, INJECTION_PATTERNS, clean_source_text, parse_ordinal
from legal_core.temporal import in_force_on

log = logging.getLogger(__name__)

_ARTICLE_MENTION = re.compile(r"(?:ال)?ماد[ةه]\s+")
_PARENS = re.compile(r"[\(\[].*?[\)\]]")
_LEXICAL_POOL = 100
_DOMAIN_QUERY_FORMS = {
    "commercial": {"تجاري", "تجاريه", "تجاره", "اقتصادي", "اقتصاديه"},
    "labor": {"عمل", "عمال", "عامل", "عمالي", "عماليه"},
    "data_protection": {"بيانات", "شخصيه", "خصوصيه"},
}


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
    issuing_authority: str | None = None
    status_basis: str | None = None  # "publisher_status": verified in force by the publisher on effective_from
    rank: int = 0
    rrf_score: float = 0.0
    lexical_rank: int | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    vector_similarity: float | None = None
    lexical_matches: int = 0
    lexical_coverage: float = 0.0
    topic_anchor_match: bool = False
    concept_window_match: bool = False
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
            "lexical_matches": self.lexical_matches,
            "lexical_coverage": round(self.lexical_coverage, 3),
            "topic_anchor_match": self.topic_anchor_match,
            "concept_window_match": self.concept_window_match,
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


_ARTICLE_EN = re.compile(r"Articles?\s+\(?(\d{1,4})\)?", re.IGNORECASE)


def mentioned_article_numbers(text_: str) -> set[str]:
    return set(extract_article_hints(text_)) | set(_ARTICLE_EN.findall(text_))


def _status_basis(version: DocumentVersion) -> str | None:
    gate = (version.validation_evidence or {}).get("gate") or {}
    evidence = (gate.get("effective_date") or {}).get("evidence") or []
    return "publisher_status" if any(e.get("kind") == "publisher_status" for e in evidence) else None


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


def _concept_matches(concepts: list[set[str]], haystack: set[str]) -> int:
    return sum(bool(forms & haystack) for forms in concepts)


def _topic_anchors(concepts: list[set[str]], lexical_rows: list[set[str]]) -> list[set[str]]:
    """Choose the least-common query concept(s) as the topic identity gate.

    PostgreSQL's simple ts_rank has no IDF. Without this extra gate, two generic words can make
    an unrelated long regulation look relevant while the actual topic word is completely absent.
    A real semantic embedding may still pass independently when it recognizes a paraphrase.
    """
    if not concepts:
        return []
    frequencies = [sum(bool(forms & row) for row in lexical_rows) for forms in concepts]
    # A paraphrase may include a word that does not occur verbatim anywhere in the corpus
    # (for example "ما حكم ..." while the operative text starts with "يحظر"). Such a zero-hit
    # concept cannot identify a topic and must not veto all otherwise exact matches.
    positive = [frequency for frequency in frequencies if frequency > 0]
    if not positive:
        return []
    least = min(positive)
    return [forms for forms, frequency in zip(concepts, frequencies, strict=True) if frequency == least]


def _concepts_share_window(concepts: list[set[str]], tokens: list[str], size: int = 32) -> bool:
    """Require all query concepts to describe one local passage, not distant parts of a long law."""
    if not concepts:
        return False
    for start in range(len(tokens)):
        window = set(tokens[start : start + size])
        if all(forms & window for forms in concepts):
            return True
    return False


def _near_token(left: str, right: str) -> bool:
    """Conservative typo/inflection match for an instrument title, not passage evidence."""
    return left == right or (
        len(left) >= 5 and len(right) >= 5 and SequenceMatcher(None, left, right).ratio() >= 0.82
    )


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


def _instrument_hints(session: Session, tokens: set[str], domain: str | None) -> set[uuid.UUID]:
    """Approved instruments named in the question, including scheduled future versions.

    Title/entity matching is deliberately independent of effective time. Otherwise a question
    naming a scheduled instrument can silently drift to an unrelated instrument that happens to
    be in force and shares generic words such as "licence".
    """
    stmt = (
        select(LegalInstrument.id, LegalInstrument.official_title_ar)
        .join(DocumentVersion, DocumentVersion.instrument_id == LegalInstrument.id)
        .where(DocumentVersion.review_status == "approved", DocumentVersion.index_active.is_(True))
        .distinct()
    )
    if domain:
        stmt = stmt.where(LegalInstrument.domain == domain)
    hinted = set()
    for instrument_id, title in session.execute(stmt):
        title_tokens = set(lexical_tokens(_PARENS.sub(" ", title)))
        matched = sum(any(_near_token(title_token, query_token) for query_token in tokens) for title_token in title_tokens)
        if title_tokens and matched / len(title_tokens) >= 0.5:
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
    # Users sometimes describe the source of an answer conversationally ("حسب تعليمات
    # المساعد الآلي").  That phrase is not part of the requested legal rule and, worse,
    # can make an injected passage the rarest lexical match.  Remove it only as a complete
    # assistant-meta phrase; standalone questions about official instructions still work.
    # In Saudi conversational usage "كم مخالفة؟" asks for the monetary fine, not for a
    # definition of the violation.  Rewrite only that question pattern; the stored text and
    # all other uses of "مخالفة" remain untouched.
    if "كم" in normalize_ar(query).split() and "مخالفه" in tokens:
        tokens = ["غرامه" if token == "مخالفه" else token for token in tokens]
    retrieval_tokens = list(tokens)
    if {"تعليمات", "مساعد"}.issubset(tokens):
        tokens = [token for token in tokens if token not in {"تعليمات", "مساعد"}]
    concepts = [query_token_forms(token) for token in tokens]
    # Search the full wording so a matching hostile passage remains visible in the
    # diagnostic trace and can trigger the exclusion warning, while evidence gating
    # uses the legal concepts after removing assistant-meta wording above.
    retrieval_concepts = [query_token_forms(token) for token in retrieval_tokens]
    search_tokens = set().union(*retrieval_concepts) if retrieval_concepts else set()
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
    lexical_token_rows: list[set[str]] = []
    best_lexical_tokens: dict[uuid.UUID, list[str]] = {}
    if tokens:
        tsq = func.to_tsquery("simple", " | ".join(sorted(search_tokens)))
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
            rows,
            key=lambda r: (
                -_concept_matches(concepts, set(r.lexical_text.split())),
                -float(r[4]),
                str(r[0]),
            ),
        )
        lexical_token_rows = [set(row.lexical_text.split()) for row in rows]
        for article_id, instrument_id, chunk_text, lex, rank_score in scored:
            instrument_of[article_id] = instrument_id
            best_chunk.setdefault(article_id, chunk_text)
            best_lexical_tokens.setdefault(article_id, lex.split())
            lexical_scores.setdefault(article_id, float(rank_score))
            lexical_ids.append(article_id)
        lexical_ids = _dedupe(lexical_ids)[: settings.retrieval_candidate_k]
    topic_anchors = _topic_anchors(concepts, lexical_token_rows)

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
    hinted_instruments = _instrument_hints(session, search_tokens, domain) if tokens else set()
    result.instrument_hints = [str(i) for i in hinted_instruments]
    pool = _dedupe(lexical_ids + vector_ids)
    if hinted_instruments:
        named_pool = [a for a in pool if instrument_of[a] in hinted_instruments]
        # The user named an approved instrument, but it has no version in force on this date.
        # Return the identity hint without unrelated passages; the answer layer will report its
        # scheduled effective date when one exists.
        if not named_pool:
            return result
        named = set(named_pool)
        rankings = [[article_id for article_id in ranking if article_id in named] for ranking in rankings]
        rankings = [ranking for ranking in rankings if ranking]
        rankings.append(named_pool)
        pool = named_pool
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
    best_lexical = lexical_ids[0] if lexical_ids else None
    if best_lexical is not None and best_lexical not in top_ids:
        top_ids.append(best_lexical)
    # Keep one instruction-like lexical candidate in diagnostics even when it falls just
    # outside the answer top-k.  It remains quarantined below and can never become evidence,
    # but retaining it lets the answer disclose that unsafe source text was excluded.
    quarantined_candidate = next(
        (
            article_id
            for article_id in lexical_ids
            if INJECTION_PATTERNS.search(best_chunk.get(article_id, ""))
        ),
        None,
    )
    if quarantined_candidate is not None and quarantined_candidate not in top_ids:
        top_ids.append(quarantined_candidate)
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
        # Keep document-kind words (نظام/لائحة/قانون) in title metadata even though
        # they are intentionally stop words for ordinary passage ranking.  Otherwise
        # a discovery query such as "قوانين العمل" cannot be satisfied by the title
        # "نظام العمل".
        title_tokens = set(lexical_tokens(instrument.official_title_ar, drop_stopwords=False))
        metadata_tokens = title_tokens | _DOMAIN_QUERY_FORMS.get(instrument.domain, set())
        passage_concepts = [forms for forms in concepts if not (forms & metadata_tokens)]
        haystack = set(lexical_tokens(f"{article_context(instrument.official_title_ar, article)}\n{evidence_text}"))
        lexical_matches = _concept_matches(concepts, haystack | metadata_tokens)
        coverage = lexical_matches / len(concepts) if concepts else 0.0
        topic_anchor_match = not topic_anchors or any(forms & (haystack | metadata_tokens) for forms in topic_anchors)
        passage_tokens = best_lexical_tokens.get(article_id, lexical_tokens(chunk_text))
        # A statute title and its domain establish identity; the local passage must establish
        # the requested rule.  This lets "عقوبة التستر التجاري" match the penalty article even
        # though that article refers back to "النظام", while still requiring the penalty words
        # to occur in the actual passage.
        passage_token_set = set(passage_tokens)
        local_concepts = [forms for forms in passage_concepts if forms & passage_token_set]
        # A compound question may legitimately be answered by separate articles (for
        # example probation length + annual leave). Require the concepts matched by this
        # article to be local to one passage, rather than requiring every concept in the
        # whole multi-part question to occur inside every cited article.
        concept_window_match = not passage_concepts or _concepts_share_window(
            local_concepts, passage_tokens, size=64
        )
        similarity = vector_sims.get(article_id)
        # Source isolation: a passage that addresses the assistant ("ignore previous instructions ...")
        # is never used as evidence, whatever it scores. It stays visible in the trace.
        quarantined = bool(INJECTION_PATTERNS.search(evidence_text))
        matches_named_instrument = not hinted_instruments or instrument.id in hinted_instruments
        strong = matches_named_instrument and not quarantined and (
            (
                lexical_matches >= settings.min_lexical_matches
                and coverage >= settings.min_lexical_coverage
                and topic_anchor_match
                and concept_window_match
            )
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
                issuing_authority=instrument.issuing_authority or version.source_authority,
                status_basis=_status_basis(version),
                rank=rank,
                # The exact lexical winner may be retained in addition to the fused top-k
                # (important for numeric tables).  It can be absent from a filtered RRF
                # ranking after instrument hints, so use its lexical reciprocal rank safely.
                rrf_score=fused.get(article_id, 1.0 / (settings.rrf_k + (lexical_rank.get(article_id) or rank))),
                lexical_rank=lexical_rank.get(article_id),
                lexical_score=lexical_scores.get(article_id),
                vector_rank=vector_rank.get(article_id),
                vector_similarity=similarity,
                lexical_matches=lexical_matches,
                lexical_coverage=coverage,
                topic_anchor_match=topic_anchor_match,
                concept_window_match=concept_window_match,
                strong=strong,
                quarantined=quarantined,
            )
        )
    return result
