"""(Re-)build the searchable chunks of one version. Idempotent: running it twice yields the same rows."""

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from legal_core.chunking import chunk_article
from legal_core.config import Settings
from legal_core.embeddings import Embedder
from legal_core.models import Article, ArticleChunk, DocumentVersion
from legal_core.normalize import lexical_text


def article_context(title: str, article: Article) -> str:
    return " - ".join(p for p in (title, article.chapter, article.article_label) if p)


def reindex_version(session: Session, version: DocumentVersion, embedder: Embedder, settings: Settings) -> int:
    title = version.instrument.official_title_ar
    article_ids = [a.id for a in version.articles]
    rows: list[ArticleChunk] = []
    inputs: list[str] = []
    for article in version.articles:
        context = article_context(title, article)
        for index, (offset, text) in enumerate(chunk_article(article.original_text_ar, settings.max_article_chars)):
            if not text.strip():
                continue
            rows.append(
                ArticleChunk(
                    article_id=article.id,
                    chunk_index=index,
                    chunk_text=text,
                    source_offset=offset,
                    lexical_text=lexical_text(f"{context}\n{text}"),
                    embedding_model=embedder.model_name,
                )
            )
            inputs.append(f"{context}\n{text}")
    # Embed before touching existing rows so a provider failure leaves the old index intact.
    vectors = embedder.embed(inputs) if inputs else []
    for row, vector in zip(rows, vectors, strict=True):
        row.embedding = vector
    if article_ids:
        session.execute(delete(ArticleChunk).where(ArticleChunk.article_id.in_(article_ids)))
    session.add_all(rows)
    session.flush()
    return len(rows)


def chunk_count(session: Session, version: DocumentVersion) -> int:
    return session.scalar(
        select(func.count(ArticleChunk.id)).join(Article).where(Article.version_id == version.id)
    ) or 0
