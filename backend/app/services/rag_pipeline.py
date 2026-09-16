"""The end-to-end RAG orchestrator.

One entry point, `answer_question`, so that every caller — the chat API, the
evaluation harness, an admin debug endpoint — runs the identical pipeline with
the identical RBAC scoping. Divergent copies of this flow are how a system
ends up with an evaluation score that does not describe production.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import SearchLog, UserRole
from app.observability.tracing import Trace
from app.schemas import Citation, RetrievedChunk
from app.services import rerank_service, search_service
from app.services.generation_service import generate_answer
from app.services.search_service import SearchFilters

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass(slots=True)
class PipelineResult:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    answered: bool = False
    latency_ms: int = 0
    trace_id: str | None = None
    retrieved: list[RetrievedChunk] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


async def answer_question(
    session: AsyncSession,
    question: str,
    role: UserRole,
    *,
    user_id: uuid.UUID | None = None,
    filters: SearchFilters | None = None,
    log_search: bool = True,
) -> PipelineResult:
    """Retrieve, rerank, and generate a cited answer scoped to `role`.

    Args:
        session: Async DB session.
        question: The user's question.
        role: Caller's role — the only thing that determines what may be read.
        user_id: For trace and search-log attribution.
        filters: Optional department / type / recency narrowing.
        log_search: Whether to persist a `search_logs` row. The evaluation
            harness turns this off so eval runs do not pollute the analytics.

    Returns:
        A `PipelineResult`; `answered=False` when the system declined.
    """
    trace = Trace(
        name="campus-rag-query",
        user_id=str(user_id) if user_id else None,
        metadata={"role": role.value if isinstance(role, UserRole) else str(role)},
    )

    with trace.span("retrieval", input=question) as span:
        candidates = await search_service.hybrid_search(
            session, question, role, top_k=settings.retrieval_top_k, filters=filters
        )
        span.update(
            output={"count": len(candidates)},
            metadata={"top_ids": [str(c.chunk_id) for c in candidates[:5]]},
        )

    with trace.span("rerank", input={"candidates": len(candidates)}) as span:
        # The cross-encoder is synchronous and CPU-bound; running it in a worker
        # thread keeps the event loop free to serve other requests.
        reranked = await asyncio.to_thread(
            rerank_service.rerank, question, candidates, settings.rerank_top_k
        )
        span.update(
            output={
                "kept": len(reranked),
                "scores": [round(c.rerank_score or 0.0, 4) for c in reranked],
            }
        )

    with trace.span("generation", input={"context_chunks": len(reranked)}) as span:
        generated = await generate_answer(question, reranked)
        span.update(
            output={
                "answered": generated.answered,
                "confidence": generated.confidence,
                "citation_count": len(generated.citations),
            }
        )

    trace.record_generation(
        model=generated.model,
        input_tokens=generated.input_tokens,
        output_tokens=generated.output_tokens,
        latency_ms=generated.latency_ms,
        answered=generated.answered,
    )
    trace.finalize(
        output=generated.answer,
        metadata={"answered": generated.answered, "confidence": generated.confidence},
    )

    if log_search:
        # Unanswered questions are the most valuable signal in the system: each
        # one is either a retrieval bug or a genuine gap in the document corpus.
        session.add(
            SearchLog(
                user_id=user_id,
                query=question,
                top_score=generated.confidence,
                result_count=len(reranked),
                was_answered=generated.answered,
                trace_id=trace.id,
            )
        )
        await session.commit()

    return PipelineResult(
        answer=generated.answer,
        citations=generated.citations,
        confidence=generated.confidence,
        answered=generated.answered,
        latency_ms=generated.latency_ms,
        trace_id=trace.id,
        retrieved=reranked,
        input_tokens=generated.input_tokens,
        output_tokens=generated.output_tokens,
    )
