"""Registration, login and profile endpoints."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, create_access_token, hash_password, verify_password
from app.db import get_db
from app.models import User, UserRole
from app.permissions import capabilities_for
from app.schemas import LoginRequest, Token, UserCreate, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate, db: DbSession) -> User:
    """Create an account.

    Self-registration is limited to the `student` role. Elevated roles are
    granted by an administrator through the users endpoint, so a public form
    can never mint itself a professor or admin account.
    """
    existing = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")

    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=UserRole.student,
        department=payload.department,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(payload: LoginRequest, db: DbSession) -> Token:
    """Exchange credentials for a bearer token."""
    user = (
        await db.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()

    # Same error for unknown email and wrong password: distinguishing them
    # would let an attacker enumerate valid accounts.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

    token, expires_in = create_access_token(user)
    return Token(access_token=token, expires_in=expires_in, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
async def read_current_user(user: CurrentUser) -> User:
    return user


@router.get("/me/capabilities")
async def read_capabilities(user: CurrentUser) -> dict[str, object]:
    """Capabilities for the current role, so the UI can hide what the API would reject."""
    role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
    return {"role": role.value, "capabilities": list(capabilities_for(role))}
