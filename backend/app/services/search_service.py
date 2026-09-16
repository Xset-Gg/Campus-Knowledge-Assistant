"""Hybrid retrieval: pgvector semantic search + PostgreSQL full-text search.

Why hybrid
----------
Semantic search alone misses exact identifiers — a student asking about
"CS101" gets chunks about introductory computing generally, because the
embedding of a course code carries little meaning. Lexical search alone misses
paraphrase — "can I hand in work late" never matches a policy titled
"Assignment Submission Deadlines". Running both and fusing the rankings
recovers each one's blind spot.

Fusion
------
Default is Reciprocal Rank Fusion, which combines *ranks* rather than scores
and so needs no score normalization between two incomparable scales (cosine
distance vs. ts_rank_cd). Weighted score fusion is available as an alternative
when you want to tune the balance continuously.

Security
--------
Every query runs through `_build_access_filter`, which restricts rows to the
access levels the caller's role permits. The filter is part of both CTEs, so
a restricted chunk is never even a fusion candidate. All user input is bound
as a parameter — no query text is ever interpolated into SQL.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AccessLevel, DocumentType, UserRole
from app.permissions import allowed_access_level_values
from app.schemas import RetrievedChunk
from app.services.embedding_service import embed_query

logger = logging.getLogger(__name__)
settings = get_settings()


class FusionStrategy(str, Enum):
    rrf = "rrf"
    weighted = "weighted"


@dataclass(slots=True)
class SearchFilters:
    """Optional, user-supplied narrowing on top of the mandatory RBAC filter."""

    department: str | None = None
    document_type: DocumentType | None = None
    academic_year: int | None = None
    include_outdated: bool = False
    only_current_documents: bool = True


def _build_access_filter(role: UserRole, params: dict[str, Any]) -> str:
    """Return the mandatory RBAC predicate and bind its parameter.

    This is the security boundary: it is applied to every retrieval CTE.
    """
    params["allowed_levels"] = allowed_access_level_values(role)
    return "c.access_level = ANY(:allowed_levels)"


def _build_optional_filters(filters: SearchFilters, params: dict[str, Any]) -> str:
    """Build the non-security predicates (department, type, recency)."""
    clauses: list[str] = []

    if filters.department:
        params["department"] = filters.department
        clauses.append("c.department = :department")

    if filters.document_type:
        params["document_type"] = filters.document_type.value
        clauses.append("c.document_type = :document_type")

    if filters.academic_year:
        params["academic_year"] = filters.academic_year
        clauses.append("c.academic_year = :academic_year")

    if not filters.include_outdated:
        # Superseded documents stay in the corpus (admins may need them) but are
        # excluded from ordinary retrieval unless explicitly requested.
        clauses.append("d.is_current = TRUE")

    return (" AND " + " AND ".join(clauses)) if clauses else ""


# Temporal relevance: documents from the current academic year are boosted;
# older ones decay so a 2024 policy loses to its 2026 replacement on a tie.
_TEMPORAL_BOOST_SQL = """
    CASE
        WHEN c.academic_year >= :current_year THEN 1.0
        WHEN c.academic_year = :current_year - 1 THEN 0.85
        WHEN c.academic_year = :current_year - 2 THEN 0.70
        ELSE 0.55
    END
"""

_HYBRID_SQL_TEMPLATE = """
WITH semantic AS (
    SELECT
        c.id,
        ROW_NUMBER() OVER (ORDER BY c.embedding <=> CAST(:query_embedding AS vector)) AS rank,
        1 - (c.embedding <=> CAST(:query_embedding AS vector)) AS score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE {access_filter}
      AND c.embedding IS NOT NULL
      {optional_filters}
    ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
    LIMIT :candidate_pool
),
keyword AS (
    SELECT
        c.id,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', :query_text)) DESC
        ) AS rank,
        ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', :query_text)) AS score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE {access_filter}
      AND c.content_tsv @@ websearch_to_tsquery('english', :query_text)
      {optional_filters}
    ORDER BY score DESC
    LIMIT :candidate_pool
),
fused AS (
    SELECT
        COALESCE(s.id, k.id) AS chunk_id,
        s.rank  AS semantic_rank,
        k.rank  AS keyword_rank,
        s.score AS semantic_score,
        k.score AS keyword_score,
        {fusion_expression} AS base_score
    FROM semantic s
    FULL OUTER JOIN keyword k ON s.id = k.id
)
SELECT
    f.chunk_id,
    f.semantic_rank,
    f.keyword_rank,
    f.semantic_score,
    f.keyword_score,
    f.base_score * ({temporal_boost}) AS fusion_score,
    c.content,
    c.page_number,
    c.section_path,
    c.department,
    c.academic_year,
    c.document_type,
    c.access_level,
    d.id    AS document_id,
    d.title AS document_title,
    d.source_url
FROM fused f
JOIN chunks c    ON c.id = f.chunk_id
JOIN documents d ON d.id = c.document_id
ORDER BY fusion_score DESC
LIMIT :top_k
"""

# Reciprocal Rank Fusion. `rrf_k` damps the influence of top ranks so a single
# list cannot dominate; 60 is the value from the original RRF paper.
_RRF_EXPRESSION = """
    (:semantic_weight * COALESCE(1.0 / (:rrf_k + s.rank), 0.0))
  + (:keyword_weight  * COALESCE(1.0 / (:rrf_k + k.rank), 0.0))
"""

# Weighted score fusion. Keyword scores are squashed into [0,1) because
# ts_rank_cd is unbounded, making it comparable to cosine similarity.
_WEIGHTED_EXPRESSION = """
    (:semantic_weight * COALESCE(s.score, 0.0))
  + (:keyword_weight  * COALESCE(k.score / (1.0 + k.score), 0.0))
"""


async def hybrid_search(
    session: AsyncSession,
    query: str,
    role: UserRole,
    *,
    top_k: int | None = None,
    filters: SearchFilters | None = None,
    strategy: FusionStrategy = FusionStrategy.rrf,
    candidate_pool: int | None = None,
) -> list[RetrievedChunk]:
    """Run hybrid retrieval scoped to what `role` is allowed to read.

    Args:
        session: Async DB session.
        query: Raw user question. Bound as a parameter; never interpolated.
        role: Caller's role — determines the access-level filter.
        top_k: Number of fused results to return.
        filters: Optional department / type / recency narrowing.
        strategy: `rrf` (default) or `weighted` score fusion.
        candidate_pool: How many candidates each retriever contributes before
            fusion. Larger values improve recall at some latency cost.

    Returns:
        Fused results ordered by score, highest first. Empty when nothing
        matches — callers must handle that rather than assuming a hit.
    """
    query = query.strip()
    if not query:
        return []

    filters = filters or SearchFilters()
    top_k = top_k or settings.retrieval_top_k
    candidate_pool = candidate_pool or max(top_k * 3, 50)

    try:
        query_embedding = embed_query(query)
    except Exception:
        logger.exception("Query embedding failed; falling back to keyword-only search")
        return await keyword_only_search(session, query, role, top_k=top_k, filters=filters)

    params: dict[str, Any] = {
        "query_text": query,
        # pgvector accepts the bracketed literal form; CAST(... AS vector) in SQL
        # turns it into a vector without needing a driver-level type codec.
        "query_embedding": "[" + ",".join(f"{value:.8f}" for value in query_embedding) + "]",
        "top_k": top_k,
        "candidate_pool": candidate_pool,
        "rrf_k": settings.rrf_k,
        "semantic_weight": settings.hybrid_semantic_weight,
        "keyword_weight": settings.hybrid_keyword_weight,
        "current_year": settings.current_academic_year,
    }

    access_filter = _build_access_filter(role, params)
    optional_filters = _build_optional_filters(filters, params)
    fusion_expression = _RRF_EXPRESSION if strategy is FusionStrategy.rrf else _WEIGHTED_EXPRESSION

    sql = _HYBRID_SQL_TEMPLATE.format(
        access_filter=access_filter,
        optional_filters=optional_filters,
        fusion_expression=fusion_expression,
        temporal_boost=_TEMPORAL_BOOST_SQL,
    )

    result = await session.execute(text(sql), params)
    rows = result.mappings().all()
    return [_row_to_chunk(row) for row in rows]


async def keyword_only_search(
    session: AsyncSession,
    query: str,
    role: UserRole,
    *,
    top_k: int = 10,
    filters: SearchFilters | None = None,
) -> list[RetrievedChunk]:
    """Lexical-only fallback, used when the embedding model is unavailable."""
    filters = filters or SearchFilters()
    params: dict[str, Any] = {
        "query_text": query,
        "top_k": top_k,
        "current_year": settings.current_academic_year,
    }
    access_filter = _build_access_filter(role, params)
    optional_filters = _build_optional_filters(filters, params)

    sql = f"""
    SELECT
        c.id AS chunk_id,
        NULL::bigint AS semantic_rank,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', :query_text)) DESC
        ) AS keyword_rank,
        NULL::float AS semantic_score,
        ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', :query_text)) AS keyword_score,
        ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', :query_text))
            * ({_TEMPORAL_BOOST_SQL}) AS fusion_score,
        c.content,
        c.page_number,
        c.section_path,
        c.department,
        c.academic_year,
        c.document_type,
        c.access_level,
        d.id AS document_id,
        d.title AS document_title,
        d.source_url
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE {access_filter}
      AND c.content_tsv @@ websearch_to_tsquery('english', :query_text)
      {optional_filters}
    ORDER BY fusion_score DESC
    LIMIT :top_k
    """
    result = await session.execute(text(sql), params)
    return [_row_to_chunk(row) for row in result.mappings().all()]


def _row_to_chunk(row: Any) -> RetrievedChunk:
    """Map a raw result row onto the typed retrieval model."""
    return RetrievedChunk(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        document_title=row["document_title"],
        content=row["content"],
        page_number=row["page_number"],
        section_path=row["section_path"],
        department=row["department"],
        academic_year=row["academic_year"],
        document_type=DocumentType(row["document_type"]),
        access_level=AccessLevel(row["access_level"]),
        source_url=row["source_url"],
        semantic_rank=int(row["semantic_rank"]) if row["semantic_rank"] is not None else None,
        keyword_rank=int(row["keyword_rank"]) if row["keyword_rank"] is not None else None,
        fusion_score=float(row["fusion_score"] or 0.0),
    )


async def get_chunk_by_id(
    session: AsyncSession, chunk_id: uuid.UUID, role: UserRole
) -> RetrievedChunk | None:
    """Fetch one chunk by id, still subject to the caller's access level."""
    params: dict[str, Any] = {"chunk_id": str(chunk_id)}
    access_filter = _build_access_filter(role, params)
    sql = f"""
    SELECT
        c.id AS chunk_id, NULL::bigint AS semantic_rank, NULL::bigint AS keyword_rank,
        NULL::float AS semantic_score, NULL::float AS keyword_score, 0.0 AS fusion_score,
        c.content, c.page_number, c.section_path, c.department, c.academic_year,
        c.document_type, c.access_level,
        d.id AS document_id, d.title AS document_title, d.source_url
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE c.id = CAST(:chunk_id AS uuid) AND {access_filter}
    """
    row = (await session.execute(text(sql), params)).mappings().first()
    return _row_to_chunk(row) if row else None
