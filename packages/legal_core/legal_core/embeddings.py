import hashlib
import math
from typing import Protocol

from legal_core.config import Settings
from legal_core.normalize import lexical_tokens


class Embedder(Protocol):
    model_name: str
    is_semantic: bool

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Deterministic offline embedding (hashed stems + character trigrams).

    It makes the stack runnable and tests reproducible without an API key. It only
    captures surface overlap, so its similarity scores are not used for evidence gating.
    """

    is_semantic = False

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.model_name = f"hash-ngram-{dim}"

    def _features(self, text: str) -> list[str]:
        tokens = lexical_tokens(text)
        grams = [tok[i : i + 3] for tok in tokens for i in range(max(1, len(tok) - 2))]
        return tokens + tokens + grams  # whole stems weigh double

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dim
                vec[bucket] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm == 0:
                vec[0] = 1.0  # pgvector cosine distance is undefined for zero vectors
            else:
                vec = [v / norm for v in vec]
            vectors.append(vec)
        return vectors


class OpenAIEmbedder:
    is_semantic = True

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        if not settings.openai_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        self._client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        self.model_name = settings.embedding_model
        self._dim = settings.embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            resp = self._client.embeddings.create(
                model=self.model_name, input=texts[i : i + 64], dimensions=self._dim
            )
            out.extend(item.embedding for item in resp.data)
        return out


def get_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings)
    if settings.embedding_provider == "hash":
        return HashEmbedder(settings.embedding_dim)
    raise RuntimeError(f"unknown EMBEDDING_PROVIDER: {settings.embedding_provider}")
