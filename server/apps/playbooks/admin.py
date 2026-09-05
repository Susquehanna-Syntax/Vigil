from django.contrib import admin

from .models import Playbook, PlaybookStep


class PlaybookStepInline(admin.TabularInline):
    model = PlaybookStep
    extra = 0


@admin.register(Playbook)
class PlaybookAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "target_tags", "created_by", "created_at")
    list_filter = ("enabled",)
    inlines = [PlaybookStepInline]
