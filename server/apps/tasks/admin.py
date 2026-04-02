from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("action", "host", "state", "risk_level", "requested_by", "created_at")
    list_filter = ("state", "risk_level", "action")
    search_fields = ("host__hostname", "action")
    readonly_fields = ("id", "nonce", "signature", "created_at")
