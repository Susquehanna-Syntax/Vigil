from django.contrib import admin

from .models import KevEntry, RemediationPolicy, VulnException, VulnSummary


@admin.register(VulnSummary)
class VulnSummaryAdmin(admin.ModelAdmin):
    list_display = ("host", "critical", "high", "medium", "low", "info", "last_scan_at", "synced_at")
    list_filter = ()
    search_fields = ("host__hostname", "host__ip_address")
    ordering = ("-critical", "-high")
    readonly_fields = ("synced_at",)


@admin.register(RemediationPolicy)
class RemediationPolicyAdmin(admin.ModelAdmin):
    list_display = ("__str__", "use_cisa_due_date", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        # Single-row settings model — get_active() owns creation.
        return not RemediationPolicy.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(KevEntry)
class KevEntryAdmin(admin.ModelAdmin):
    list_display = ("cve_id", "date_added", "cisa_due_date", "ransomware", "source")
    list_filter = ("source", "ransomware")
    search_fields = ("cve_id", "name")
    ordering = ("-date_added",)

    # Synced data, not operator-editable: an edit here would be silently
    # overwritten by the next load_kev run.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(VulnException)
class VulnExceptionAdmin(admin.ModelAdmin):
    list_display = ("finding", "kind", "expires_on", "created_by", "created_at")
    list_filter = ("kind",)
    search_fields = ("finding__cve_id", "reason")
    readonly_fields = ("created_at",)
