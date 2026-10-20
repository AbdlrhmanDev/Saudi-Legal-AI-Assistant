"""(Re-)build the searchable chunks of one version. Idempotent: running it twice yields the same rows.

Incremental (PRD v2 10.3): a chunk whose embedding input is identical to one already stored for the
same model reuses that vector, so an amendment only calls the embedding model for changed articles.
"""

import hashlib
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from legal_core.chunking import chunk_article
from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.models import Article, ArticleChunk, DocumentVersion
from legal_core.normalize import lexical_text


@dataclass
class IndexStats:
    chunks: int = 0
    embedded: int = 0
    reused: int = 0

    def __iadd__(self, other: "IndexStats") -> "IndexStats":
        self.chunks += other.chunks
        self.embedded += other.embedded
        self.reused += other.reused
        return self


def article_context(title: str, article: Article) -> str:
    return " - ".join(p for p in (title, article.chapter, article.article_label) if p)


def content_hash(model_name: str, embedding_input: str) -> str:
    return hashlib.sha256(f"{model_name}\n{embedding_input}".encode()).hexdigest()


def _reusable_vectors(session: Session, hashes: set[str]) -> dict[str, list[float]]:
    if not hashes:
        return {}
    rows = session.execute(
        select(ArticleChunk.content_hash, ArticleChunk.embedding).where(
            ArticleChunk.content_hash.in_(hashes), ArticleChunk.embedding.is_not(None)
        )
    ).all()
    return {h: list(v) for h, v in rows}


def index_version(session: Session, version: DocumentVersion, embedder: Embedder, settings: Settings) -> IndexStats:
    title = version.instrument.official_title_ar
    article_ids = [a.id for a in version.articles]
    rows: list[ArticleChunk] = []
    inputs: list[str] = []
    for article in version.articles:
        context = article_context(title, article)
        for index, (offset, text) in enumerate(chunk_article(article.original_text_ar, settings.max_article_chars)):
            if not text.strip():
                continue
            embedding_input = f"{context}\n{text}"
            rows.append(
                ArticleChunk(
                    article_id=article.id,
                    chunk_index=index,
                    chunk_text=text,
                    source_offset=offset,
                    lexical_text=lexical_text(embedding_input),
                    embedding_model=embedder.model_name,
                    content_hash=content_hash(embedder.model_name, embedding_input),
                )
            )
            inputs.append(embedding_input)

    # Reuse vectors from other versions (and from this version's own previous index rows).
    reusable = _reusable_vectors(session, {r.content_hash for r in rows})
    missing = {row.content_hash: text for row, text in zip(rows, inputs, strict=True) if row.content_hash not in reusable}
    # Embed before touching existing rows so a provider failure leaves the old index intact.
    vectors = embedder.embed(list(missing.values())) if missing else []
    fresh = dict(zip(missing, vectors, strict=True))
    for row in rows:
        row.embedding = reusable.get(row.content_hash) or fresh[row.content_hash]
    if article_ids:
        session.execute(delete(ArticleChunk).where(ArticleChunk.article_id.in_(article_ids)))
    session.add_all(rows)
    session.flush()
    reused = sum(1 for row in rows if row.content_hash in reusable)
    return IndexStats(chunks=len(rows), embedded=len(missing), reused=reused)


def reindex_version(session: Session, version: DocumentVersion, embedder: Embedder, settings: Settings) -> int:
    return index_version(session, version, embedder, settings).chunks


def chunk_count(session: Session, version: DocumentVersion) -> int:
    return session.scalar(
        select(func.count(ArticleChunk.id)).join(Article).where(Article.version_id == version.id)
    ) or 0
