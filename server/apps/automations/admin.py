from django.contrib import admin

from .models import Automation


@admin.register(Automation)
class AutomationAdmin(admin.ModelAdmin):
    list_display = ("name", "trigger", "event", "action_kind", "target", "enabled",
                    "run_count", "last_run")
    list_filter = ("trigger", "enabled", "action_kind")
