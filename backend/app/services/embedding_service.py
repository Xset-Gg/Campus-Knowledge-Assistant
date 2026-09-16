"""Sentence-transformer embedding generation.

The model is loaded lazily and cached process-wide so that the first request
pays the load cost and every subsequent call reuses the warm model.
"""
from __future__ import annotations

import logging
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)
_load_lock = threading.Lock()


@lru_cache(maxsize=1)
def _get_model() -> "SentenceTransformer":
    from sentence_transformers import SentenceTransformer

    settings = get_settings()
    with _load_lock:
        logger.info("Loading embedding model %s", settings.embedding_model)
        return SentenceTransformer(settings.embedding_model)


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed a batch of texts. Returns L2-normalized vectors for cosine similarity."""
    if not texts:
        return []
    model = _get_model()
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=False,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return [vector.tolist() for vector in vectors]


def embed_query(query: str) -> list[float]:
    """Embed a single search query."""
    return embed_texts([query])[0]


def warm_up() -> None:
    """Pre-load the model at application startup to avoid a cold first request."""
    try:
        _get_model()
    except Exception:  # pragma: no cover - startup best-effort
        logger.exception("Embedding model warm-up failed; will retry on first use")
