from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "username", "action", "target", "auth_method", "ip")
    list_filter = ("action",)
    search_fields = ("username", "target")
    date_hierarchy = "created_at"

    # Append-only: the trail is only trustworthy if nobody can edit it.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
