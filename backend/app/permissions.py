"""RBAC policy definitions and the SQL filter that enforces them.

The single rule this module exists to guarantee: a user never receives a chunk
whose `access_level` outranks their role. That filter is applied inside the
retrieval SQL itself — not as a post-filter in Python — so there is no code
path (search, ask, evaluation, admin debug) that can return over-privileged
content by forgetting a check.
"""
from __future__ import annotations

from app.models import AccessLevel, UserRole

# Which access levels each role may read.
ROLE_ACCESS_LEVELS: dict[UserRole, tuple[AccessLevel, ...]] = {
    UserRole.student: (AccessLevel.public, AccessLevel.student),
    UserRole.professor: (AccessLevel.public, AccessLevel.student, AccessLevel.faculty),
    UserRole.admin: (AccessLevel.public, AccessLevel.student, AccessLevel.faculty, AccessLevel.admin),
}

# Capability flags surfaced to the frontend so the UI can hide what the API
# would reject anyway. The API remains the enforcement point.
ROLE_CAPABILITIES: dict[UserRole, tuple[str, ...]] = {
    UserRole.student: ("ask", "feedback", "view_history"),
    UserRole.professor: ("ask", "feedback", "view_history", "view_faculty_docs", "upload_documents"),
    UserRole.admin: (
        "ask",
        "feedback",
        "view_history",
        "view_faculty_docs",
        "upload_documents",
        "manage_documents",
        "view_analytics",
        "view_failed_searches",
        "manage_users",
        "debug_retrieval",
    ),
}


def allowed_access_levels(role: UserRole) -> tuple[AccessLevel, ...]:
    """Access levels readable by `role`. Unknown roles get the most restrictive set."""
    return ROLE_ACCESS_LEVELS.get(role, (AccessLevel.public,))


def allowed_access_level_values(role: UserRole) -> list[str]:
    """Access levels as raw strings, for binding into SQL parameters."""
    return [level.value for level in allowed_access_levels(role)]


def capabilities_for(role: UserRole) -> tuple[str, ...]:
    return ROLE_CAPABILITIES.get(role, ())


def has_capability(role: UserRole, capability: str) -> bool:
    return capability in capabilities_for(role)


def can_read(role: UserRole, access_level: AccessLevel) -> bool:
    """Whether `role` may read a resource at `access_level`."""
    return access_level in allowed_access_levels(role)
