from django.contrib import admin

from .models import Task, TaskDefinition, TaskRun


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("action", "host", "state", "risk_level", "requested_by", "created_at")
    list_filter = ("state", "risk_level", "action")
    search_fields = ("host__hostname", "action")
    readonly_fields = ("id", "nonce", "signature", "created_at")


@admin.register(TaskDefinition)
class TaskDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "visibility", "risk_level", "updated_at")
    list_filter = ("visibility", "risk_level")
    search_fields = ("name", "description", "owner__username")
    readonly_fields = ("id", "parsed_spec", "created_at", "updated_at")


@admin.register(TaskRun)
class TaskRunAdmin(admin.ModelAdmin):
    list_display = ("name_snapshot", "requested_by", "state", "host_count", "step_count", "created_at")
    list_filter = ("state",)
    search_fields = ("name_snapshot", "requested_by__username")
    readonly_fields = ("id", "created_at", "finished_at")
