from rest_framework.permissions import BasePermission

from .models import Role


def role_of(user) -> str:
    """The effective role. ``is_staff``/``is_superuser`` are ADMIN regardless
    of profile so the original single-admin install never locks itself out;
    everyone else reads their profile (VIEWER when no profile exists —
    least-privilege default)."""
    if not (user and user.is_authenticated):
        return ""
    if user.is_staff or user.is_superuser:
        return Role.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile else Role.VIEWER


class IsAdmin(BasePermission):
    """Server-side admin gate for administrative endpoints.

    Any user who isn't an admin must not be able to upload agent binaries,
    change settings, or decide enrollments. RBAC maps ADMIN onto
    is_staff/profile.role through the same check, so every caller agrees on
    what "admin" means.
    """

    message = "Administrator access required."

    def has_permission(self, request, view):
        return role_of(request.user) == Role.ADMIN


class IsOperator(BasePermission):
    """Operator-or-better: may deploy/execute tasks and acknowledge alerts,
    but not change settings or approve hosts (those stay IsAdmin). The
    OPERATOR role itself is only assignable under a Business license
    (``rbac_advanced``); this check stays free because a licensed install's
    operators must keep working after a lapse — §6: degrade features, never
    lock humans out mid-incident."""

    message = "Operator access required."

    def has_permission(self, request, view):
        return role_of(request.user) in (Role.ADMIN, Role.OPERATOR)
