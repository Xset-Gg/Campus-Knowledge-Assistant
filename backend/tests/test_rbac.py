"""RBAC and access-scoping tests.

These assert the property the whole system rests on: a role never receives
content above its access level, and the filter is generated as SQL rather than
applied after the fact.
"""
from __future__ import annotations

import pytest

from app.models import AccessLevel, UserRole
from app.permissions import (
    allowed_access_level_values,
    allowed_access_levels,
    can_read,
    capabilities_for,
    has_capability,
)
from app.services.search_service import SearchFilters, _build_access_filter, _build_optional_filters


class TestAccessLevels:
    def test_student_cannot_read_faculty_or_admin(self):
        levels = allowed_access_levels(UserRole.student)
        assert AccessLevel.public in levels
        assert AccessLevel.student in levels
        assert AccessLevel.faculty not in levels
        assert AccessLevel.admin not in levels

    def test_professor_cannot_read_admin(self):
        levels = allowed_access_levels(UserRole.professor)
        assert AccessLevel.faculty in levels
        assert AccessLevel.admin not in levels

    def test_admin_reads_everything(self):
        assert set(allowed_access_levels(UserRole.admin)) == set(AccessLevel)

    @pytest.mark.parametrize(
        ("role", "level", "expected"),
        [
            (UserRole.student, AccessLevel.public, True),
            (UserRole.student, AccessLevel.student, True),
            (UserRole.student, AccessLevel.faculty, False),
            (UserRole.student, AccessLevel.admin, False),
            (UserRole.professor, AccessLevel.faculty, True),
            (UserRole.professor, AccessLevel.admin, False),
            (UserRole.admin, AccessLevel.admin, True),
        ],
    )
    def test_can_read_matrix(self, role, level, expected):
        assert can_read(role, level) is expected


class TestCapabilities:
    def test_student_has_no_admin_capabilities(self):
        caps = capabilities_for(UserRole.student)
        for forbidden in ("view_analytics", "manage_users", "manage_documents", "debug_retrieval"):
            assert forbidden not in caps

    def test_professor_cannot_manage_users(self):
        assert not has_capability(UserRole.professor, "manage_users")
        assert has_capability(UserRole.professor, "upload_documents")

    def test_admin_has_all_capabilities(self):
        for capability in ("view_analytics", "manage_users", "manage_documents", "debug_retrieval"):
            assert has_capability(UserRole.admin, capability)


class TestSearchFilterGeneration:
    def test_access_filter_binds_a_parameter_and_never_interpolates(self):
        params: dict = {}
        clause = _build_access_filter(UserRole.student, params)
        assert clause == "c.access_level = ANY(:allowed_levels)"
        assert params["allowed_levels"] == ["public", "student"]
        # The role's levels must not be spliced into the SQL text itself.
        assert "student'" not in clause

    def test_access_filter_is_role_specific(self):
        student_params: dict = {}
        admin_params: dict = {}
        _build_access_filter(UserRole.student, student_params)
        _build_access_filter(UserRole.admin, admin_params)
        assert "admin" not in student_params["allowed_levels"]
        assert "admin" in admin_params["allowed_levels"]

    def test_unknown_role_defaults_to_public_only(self):
        assert allowed_access_level_values("not-a-role") == ["public"]  # type: ignore[arg-type]

    def test_outdated_documents_excluded_by_default(self):
        params: dict = {}
        clause = _build_optional_filters(SearchFilters(), params)
        assert "d.is_current = TRUE" in clause

    def test_include_outdated_drops_the_recency_clause(self):
        params: dict = {}
        clause = _build_optional_filters(SearchFilters(include_outdated=True), params)
        assert "is_current" not in clause

    def test_user_supplied_department_is_parameterized(self):
        params: dict = {}
        clause = _build_optional_filters(
            SearchFilters(department="Robert'); DROP TABLE chunks;--"), params
        )
        assert "c.department = :department" in clause
        assert "DROP TABLE" not in clause
        assert params["department"] == "Robert'); DROP TABLE chunks;--"
