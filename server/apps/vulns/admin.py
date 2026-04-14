from django.contrib import admin

from .models import VulnSummary


@admin.register(VulnSummary)
class VulnSummaryAdmin(admin.ModelAdmin):
    list_display = ("host", "critical", "high", "medium", "low", "info", "last_scan_at", "synced_at")
    list_filter = ()
    search_fields = ("host__hostname", "host__ip_address")
    ordering = ("-critical", "-high")
    readonly_fields = ("synced_at",)
