from django.contrib import admin

from .models import ADConfig, Host, HostInventory


@admin.register(Host)
class HostAdmin(admin.ModelAdmin):
    list_display = ("hostname", "status", "mode", "ip_address", "last_checkin")
    list_filter = ("status", "mode")
    search_fields = ("hostname", "ip_address")
    readonly_fields = ("id", "agent_token", "created_at", "updated_at")


@admin.register(HostInventory)
class HostInventoryAdmin(admin.ModelAdmin):
    list_display = ("host", "manufacturer", "model_name", "service_tag", "cpu_cores", "updated_at")
    search_fields = ("host__hostname", "service_tag", "manufacturer", "model_name")
    readonly_fields = ("updated_at",)


@admin.register(ADConfig)
class ADConfigAdmin(admin.ModelAdmin):
    list_display = ("ldap_url", "enabled", "last_sync", "last_sync_status")
    readonly_fields = ("bind_password_encrypted", "last_sync", "last_sync_status", "created_at", "updated_at")
