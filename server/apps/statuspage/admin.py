from django.contrib import admin

from .models import StatusPage


@admin.register(StatusPage)
class StatusPageAdmin(admin.ModelAdmin):
    list_display = ("title", "enabled", "token", "site_id", "created_at")
