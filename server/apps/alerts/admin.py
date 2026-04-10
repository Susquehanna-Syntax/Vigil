from django.contrib import admin

from .models import Alert, AlertRule, NotificationChannel


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "metric", "operator", "threshold", "severity", "enabled")
    list_filter = ("severity", "enabled", "is_default")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("host", "severity", "state", "message", "fired_at")
    list_filter = ("state", "severity")
    search_fields = ("host__hostname", "message")


@admin.register(NotificationChannel)
class NotificationChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "enabled", "on_firing", "on_resolved")
    list_filter = ("kind", "enabled")
