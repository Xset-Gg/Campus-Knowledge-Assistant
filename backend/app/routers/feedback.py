"""Thumbs up/down feedback collection for RLHF and quality monitoring."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.auth import CurrentUser
from app.db import get_db
from app.models import ChatMessage, ChatSession, Feedback
from app.observability.tracing import score_trace
from app.schemas import FeedbackCreate, FeedbackOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/feedback", tags=["feedback"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED)
async def submit_feedback(payload: FeedbackCreate, user: CurrentUser, db: DbSession) -> Feedback:
    """Rate an assistant answer. Re-rating the same message replaces the prior rating."""
    if payload.rating not in (-1, 1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Rating must be 1 or -1"
        )

    # A user may only rate a message in one of their own sessions.
    message = (
        await db.execute(
            select(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatMessage.id == payload.message_id, ChatSession.user_id == user.id)
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if message.role != "assistant":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only assistant answers can be rated",
        )

    statement = (
        insert(Feedback)
        .values(
            message_id=payload.message_id,
            user_id=user.id,
            rating=payload.rating,
            comment=payload.comment,
        )
        .on_conflict_do_update(
            index_elements=[Feedback.message_id, Feedback.user_id],
            set_={"rating": payload.rating, "comment": payload.comment},
        )
        .returning(Feedback)
    )
    feedback = (await db.execute(statement)).scalar_one()
    await db.commit()

    # Mirror the rating onto the Langfuse trace so answer quality is visible
    # alongside the retrieval and generation spans that produced it.
    if message.trace_id:
        score_trace(
            message.trace_id,
            name="user_feedback",
            value=float(payload.rating),
            comment=payload.comment,
        )

    return feedback


@router.get("/message/{message_id}", response_model=FeedbackOut | None)
async def get_my_feedback(message_id: str, user: CurrentUser, db: DbSession) -> Feedback | None:
    """The caller's own rating for a message, if any."""
    return (
        await db.execute(
            select(Feedback).where(Feedback.message_id == message_id, Feedback.user_id == user.id)
        )
    ).scalar_one_or_none()
