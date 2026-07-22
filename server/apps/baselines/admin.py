from django.contrib import admin

from .models import Baseline, BaselineStep


class BaselineStepInline(admin.TabularInline):
    model = BaselineStep
    extra = 0


@admin.register(Baseline)
class BaselineAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "target_tags", "created_by", "created_at")
    list_filter = ("enabled",)
    inlines = [BaselineStepInline]
