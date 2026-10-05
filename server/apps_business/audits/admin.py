from django.contrib import admin

from vigil.licensing import has_feature

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "username", "action", "target", "auth_method", "ip")
    list_filter = ("action",)
    search_fields = ("username", "target")
    date_hierarchy = "created_at"

    def has_view_permission(self, request, obj=None):
        # Per request, not at registration: a licence lapses or is applied while
        # the process is running, and the admin site is imported once at startup.
        return has_feature("audit_log") and super().has_view_permission(request, obj)

    def has_module_permission(self, request):
        # Hides the whole Audits section from the admin index, so an unlicensed
        # install does not advertise a trail it will then refuse to show.
        return has_feature("audit_log") and super().has_module_permission(request)

    # Append-only: the trail is only trustworthy if nobody can edit it.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
