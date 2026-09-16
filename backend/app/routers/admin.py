"""Admin-only analytics, failed-search review, and user management."""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_capability
from app.db import get_db
from app.models import Chunk, Document, Feedback, SearchLog, User, UserRole
from app.schemas import AnalyticsSummary, FailedSearchOut, UserOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "/analytics",
    response_model=AnalyticsSummary,
    dependencies=[Depends(require_capability("view_analytics"))],
)
async def analytics(db: DbSession) -> AnalyticsSummary:
    """Headline usage and quality metrics."""
    total = (await db.execute(select(func.count(SearchLog.id)))).scalar_one()
    unanswered = (
        await db.execute(select(func.count(SearchLog.id)).where(SearchLog.was_answered.is_(False)))
    ).scalar_one()
    thumbs_up = (
        await db.execute(select(func.count(Feedback.id)).where(Feedback.rating == 1))
    ).scalar_one()
    thumbs_down = (
        await db.execute(select(func.count(Feedback.id)).where(Feedback.rating == -1))
    ).scalar_one()

    from app.models import ChatMessage

    avg_latency = (
        await db.execute(
            select(func.avg(ChatMessage.latency_ms)).where(ChatMessage.latency_ms.isnot(None))
        )
    ).scalar_one()

    document_count = (await db.execute(select(func.count(Document.id)))).scalar_one()
    chunk_count = (await db.execute(select(func.count(Chunk.id)))).scalar_one()

    return AnalyticsSummary(
        total_questions=total,
        unanswered_questions=unanswered,
        answer_rate=round((total - unanswered) / total, 4) if total else 0.0,
        thumbs_up=thumbs_up,
        thumbs_down=thumbs_down,
        avg_latency_ms=round(float(avg_latency), 2) if avg_latency is not None else None,
        document_count=document_count,
        chunk_count=chunk_count,
    )


@router.get(
    "/failed-searches",
    response_model=list[FailedSearchOut],
    dependencies=[Depends(require_capability("view_failed_searches"))],
)
async def failed_searches(db: DbSession, limit: int = 100) -> list[SearchLog]:
    """Questions the assistant declined to answer.

    This is the content-gap backlog: each entry is either a retrieval problem
    to tune or a document the university has not yet published.
    """
    result = await db.execute(
        select(SearchLog)
        .where(SearchLog.was_answered.is_(False))
        .order_by(SearchLog.created_at.desc())
        .limit(min(limit, 500))
    )
    return list(result.scalars().all())


@router.get(
    "/failed-searches/top",
    dependencies=[Depends(require_capability("view_failed_searches"))],
)
async def top_failed_queries(db: DbSession, limit: int = 20) -> list[dict[str, object]]:
    """Most frequently repeated unanswered questions — prioritized gap list."""
    result = await db.execute(
        select(func.lower(SearchLog.query).label("query"), func.count(SearchLog.id).label("count"))
        .where(SearchLog.was_answered.is_(False))
        .group_by(func.lower(SearchLog.query))
        .order_by(func.count(SearchLog.id).desc())
        .limit(min(limit, 100))
    )
    return [{"query": row.query, "count": row.count} for row in result.all()]


@router.get(
    "/users",
    response_model=list[UserOut],
    dependencies=[Depends(require_capability("manage_users"))],
)
async def list_users(db: DbSession, limit: int = 100, offset: int = 0) -> list[User]:
    result = await db.execute(
        select(User).order_by(User.created_at.desc()).limit(min(limit, 500)).offset(offset)
    )
    return list(result.scalars().all())


@router.patch(
    "/users/{user_id}/role",
    response_model=UserOut,
    dependencies=[Depends(require_capability("manage_users"))],
)
async def update_user_role(
    user_id: uuid.UUID, role: UserRole, current_user: CurrentUser, db: DbSession
) -> User:
    """Change a user's role.

    An admin cannot demote themselves — that is how an installation ends up
    with zero admins and no way back in.
    """
    if user_id == current_user.id and role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot remove your own admin role"
        )

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.role = role
    await db.commit()
    await db.refresh(user)
    logger.info("Role for %s changed to %s by %s", user.email, role.value, current_user.email)
    return user


@router.patch(
    "/users/{user_id}/active",
    response_model=UserOut,
    dependencies=[Depends(require_capability("manage_users"))],
)
async def set_user_active(
    user_id: uuid.UUID, is_active: bool, current_user: CurrentUser, db: DbSession
) -> User:
    """Activate or deactivate an account."""
    if user_id == current_user.id and not is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account"
        )

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.is_active = is_active
    await db.commit()
    await db.refresh(user)
    return user


@router.get(
    "/outdated-documents",
    dependencies=[Depends(require_capability("manage_documents"))],
)
async def outdated_documents(db: DbSession) -> list[dict[str, object]]:
    """Documents from prior academic years that are still marked current.

    These are the ones most likely to give a student last year's rules.
    """
    from app.config import get_settings

    settings = get_settings()
    result = await db.execute(
        select(Document)
        .where(Document.academic_year < settings.current_academic_year, Document.is_current.is_(True))
        .order_by(Document.academic_year.asc())
    )
    return [
        {
            "id": str(document.id),
            "title": document.title,
            "department": document.department,
            "academic_year": document.academic_year,
            "document_type": str(document.document_type),
            "years_behind": settings.current_academic_year - document.academic_year,
        }
        for document in result.scalars().all()
    ]
