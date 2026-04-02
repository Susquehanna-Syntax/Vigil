from django.contrib import admin

from .models import Host


@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    list_display = ("hostname", "status", "mode", "ip_address", "last_checkin")
    list_filter = ("status", "mode")
    search_fields = ("hostname", "ip_address")
    readonly_fields = ("id", "agent_token", "created_at", "updated_at")
