from django.contrib import admin

from .models import Baseline


@admin.register(Baseline)
class BaselineAdmin(admin.ModelAdmin):
    list_display = ("definition", "enabled", "target_tags", "created_by", "created_at")
    list_filter = ("enabled",)
