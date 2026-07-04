from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """Server-side admin gate for administrative endpoints.

    Community ships single-admin (the setup superuser), so this is a no-op
    there — but any additional Django users created without staff status
    must not be able to upload agent binaries, change settings, or decide
    enrollments. Pro's RBAC maps its ADMIN role onto is_staff through the
    same check, so core and editions agree on what "admin" means.
    """

    message = "Administrator access required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_staff or user.is_superuser)
        )
