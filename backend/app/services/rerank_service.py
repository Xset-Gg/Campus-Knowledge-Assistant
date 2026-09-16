"""Cross-encoder reranking of retrieved passages.

The bi-encoder used for retrieval embeds query and passage independently, so
it can only measure coarse topical similarity. A cross-encoder reads the pair
jointly and scores actual relevance, which reliably reorders the top-k: the
passage that merely mentions "attendance" sinks below the one that states the
attendance rule. We retrieve wide (25) and rerank down to a narrow, accurate
context window (5) before generation.
"""
from __future__ import annotations

import logging
import math
import threading
from functools import lru_cache
from typing import TYPE_CHECKING

from app.config import get_settings
from app.schemas import RetrievedChunk

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)
settings = get_settings()
_load_lock = threading.Lock()


@lru_cache(maxsize=1)
def _get_reranker() -> "CrossEncoder":
    from sentence_transformers import CrossEncoder

    with _load_lock:
        logger.info("Loading cross-encoder reranker %s", settings.reranker_model)
        return CrossEncoder(settings.reranker_model, max_length=512)


def _sigmoid(value: float) -> float:
    """Map a raw cross-encoder logit into (0, 1) so it can be thresholded."""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def rerank(
    query: str,
    chunks: list[RetrievedChunk],
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    """Re-score `chunks` against `query` and return the best `top_k`.

    Falls back to the original fusion order if the reranker cannot be loaded,
    so a model failure degrades quality rather than breaking the request.
    """
    if not chunks:
        return []

    top_k = top_k or settings.rerank_top_k

    try:
        model = _get_reranker()
    except Exception:
        logger.exception("Reranker unavailable; preserving fusion order")
        return chunks[:top_k]

    pairs = [(query, chunk.content) for chunk in chunks]
    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception:
        logger.exception("Reranking failed; preserving fusion order")
        return chunks[:top_k]

    for chunk, score in zip(chunks, scores, strict=True):
        chunk.rerank_score = _sigmoid(float(score))

    ranked = sorted(chunks, key=lambda c: c.rerank_score or 0.0, reverse=True)
    return ranked[:top_k]


def confidence_from_results(chunks: list[RetrievedChunk]) -> float:
    """Overall retrieval confidence, used to decide whether to answer at all.

    Weighted toward the best passage: one strongly relevant source is enough to
    answer, whereas several weak ones usually mean the corpus does not cover
    the question.
    """
    if not chunks:
        return 0.0
    scores = [c.rerank_score for c in chunks if c.rerank_score is not None]
    if not scores:
        # No reranker scores available — fall back to the fusion score, which is
        # on a different scale, so damp it before comparing to the threshold.
        return min(1.0, max((c.fusion_score for c in chunks), default=0.0))
    top = max(scores)
    mean_top3 = sum(sorted(scores, reverse=True)[:3]) / min(3, len(scores))
    return round(0.7 * top + 0.3 * mean_top3, 4)


def warm_up() -> None:
    """Pre-load the reranker at startup."""
    try:
        _get_reranker()
    except Exception:  # pragma: no cover - startup best-effort
        logger.exception("Reranker warm-up failed; will retry on first use")
