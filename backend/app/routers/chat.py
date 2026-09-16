"""Chat endpoints: ask a question, search, and read history."""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_capability
from app.db import get_db
from app.models import ChatMessage, ChatSession, UserRole
from app.schemas import (
    AskRequest,
    AskResponse,
    ChatMessageOut,
    ChatSessionOut,
    SearchRequest,
    SearchResponse,
)
from app.services import search_service
from app.services.rag_pipeline import answer_question
from app.services.search_service import SearchFilters

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _resolve_session(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID | None, question: str
) -> ChatSession:
    """Load the caller's chat session, or start a new one.

    The ownership check is what stops a user from appending to — or reading —
    someone else's conversation by guessing a session id.
    """
    if session_id is not None:
        chat_session = (
            await db.execute(select(ChatSession).where(ChatSession.id == session_id))
        ).scalar_one_or_none()
        if chat_session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")
        if chat_session.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your chat session")
        return chat_session

    title = question[:80] + ("…" if len(question) > 80 else "")
    chat_session = ChatSession(user_id=user_id, title=title)
    db.add(chat_session)
    await db.flush()
    return chat_session


@router.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest, user: CurrentUser, db: DbSession) -> AskResponse:
    """Answer a question from university documents the caller is allowed to read."""
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)

    chat_session = await _resolve_session(db, user.id, payload.session_id, payload.question)
    db.add(ChatMessage(session_id=chat_session.id, role="user", content=payload.question))
    await db.commit()

    filters = SearchFilters(
        department=payload.department,
        document_type=payload.document_type,
        include_outdated=payload.include_outdated,
    )

    result = await answer_question(
        db, payload.question, role, user_id=user.id, filters=filters
    )

    assistant_message = ChatMessage(
        session_id=chat_session.id,
        role="assistant",
        content=result.answer,
        citations=[citation.model_dump(mode="json") for citation in result.citations],
        confidence=result.confidence,
        trace_id=result.trace_id,
        latency_ms=result.latency_ms,
    )
    db.add(assistant_message)
    await db.commit()
    await db.refresh(assistant_message)

    return AskResponse(
        message_id=assistant_message.id,
        session_id=chat_session.id,
        answer=result.answer,
        citations=result.citations,
        confidence=result.confidence,
        answered=result.answered,
        latency_ms=result.latency_ms,
        trace_id=result.trace_id,
    )


@router.post("/search", response_model=SearchResponse)
async def search(payload: SearchRequest, user: CurrentUser, db: DbSession) -> SearchResponse:
    """Hybrid search without generation — useful for browsing source passages."""
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    results = await search_service.hybrid_search(
        db,
        payload.query,
        role,
        top_k=payload.top_k,
        filters=SearchFilters(
            department=payload.department,
            document_type=payload.document_type,
            include_outdated=payload.include_outdated,
        ),
    )
    return SearchResponse(query=payload.query, results=results, total=len(results))


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(user: CurrentUser, db: DbSession) -> list[ChatSession]:
    """List the caller's own chat sessions, newest first."""
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.id)
        .order_by(ChatSession.created_at.desc())
        .limit(50)
    )
    return list(result.scalars().all())


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(
    session_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> list[ChatMessage]:
    """Read one conversation. Scoped to the caller's own sessions."""
    chat_session = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if chat_session is None or chat_session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found")

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    return list(result.scalars().all())


@router.post(
    "/debug/retrieve",
    response_model=SearchResponse,
    dependencies=[Depends(require_capability("debug_retrieval"))],
)
async def debug_retrieve(payload: SearchRequest, user: CurrentUser, db: DbSession) -> SearchResponse:
    """Admin-only view of retrieval internals: fusion ranks and rerank scores.

    Still runs under the caller's own access level — an admin debugging the
    pipeline sees admin-visible content because they are an admin, not because
    this endpoint bypasses the filter.
    """
    import asyncio

    from app.services import rerank_service

    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    candidates = await search_service.hybrid_search(
        db,
        payload.query,
        role,
        top_k=payload.top_k,
        filters=SearchFilters(
            department=payload.department,
            document_type=payload.document_type,
            include_outdated=payload.include_outdated,
        ),
    )
    reranked = await asyncio.to_thread(
        rerank_service.rerank, payload.query, candidates, payload.top_k
    )
    return SearchResponse(query=payload.query, results=reranked, total=len(reranked))
