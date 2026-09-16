"""JWT authentication and the FastAPI dependencies that enforce it.

`get_current_user` is the gate: it verifies the token signature, rejects
expired or malformed tokens, and re-loads the user from the database on every
request. Re-loading matters — a role stored in a token issued an hour ago is a
stale claim, and a deactivated account must lose access immediately rather
than when its token happens to expire.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import User, UserRole
from app.permissions import has_capability

logger = logging.getLogger(__name__)
settings = get_settings()

bearer_scheme = HTTPBearer(auto_error=False)

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _prepare_secret(password: str) -> bytes:
    """Reduce a password to a fixed 44-byte digest before bcrypt sees it.

    bcrypt silently truncates its input at 72 bytes, which would make two long
    passwords sharing a 72-byte prefix interchangeable. SHA-256 + base64 keeps
    every byte of the password significant and always fits the limit.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare_secret(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare_secret(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database — treat as a failed login, not a 500.
        logger.warning("Encountered a malformed password hash")
        return False


def create_access_token(user: User, expires_delta: timedelta | None = None) -> tuple[str, int]:
    """Issue a signed access token. Returns the token and its lifetime in seconds."""
    expires_delta = expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    expire = datetime.now(UTC) + expires_delta
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role.value if isinstance(user.role, UserRole) else str(user.role),
        "exp": expire,
        "iat": datetime.now(UTC),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        logger.debug("JWT decode failed: %s", exc)
        raise _CREDENTIALS_ERROR from exc


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the authenticated user, or reject the request."""
    if credentials is None:
        raise _CREDENTIALS_ERROR

    payload = decode_access_token(credentials.credentials)
    subject = payload.get("sub")
    if not subject:
        raise _CREDENTIALS_ERROR

    try:
        user_id = uuid.UUID(subject)
    except ValueError as exc:
        raise _CREDENTIALS_ERROR from exc

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise _CREDENTIALS_ERROR
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_capability(capability: str):
    """Dependency factory gating an endpoint on an RBAC capability.

    Usage:
        @router.get("/analytics", dependencies=[Depends(require_capability("view_analytics"))])
    """

    async def dependency(user: CurrentUser) -> User:
        role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
        if not has_capability(role, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your role ({role.value}) is not permitted to perform this action",
            )
        return user

    return dependency


def require_role(*roles: UserRole):
    """Dependency factory restricting an endpoint to specific roles."""

    async def dependency(user: CurrentUser) -> User:
        role = user.role if isinstance(user.role, UserRole) else UserRole(user.role)
        if role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient privileges for this resource",
            )
        return user

    return dependency
