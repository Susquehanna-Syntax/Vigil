from django.contrib import admin

from .models import InstanceSetting


@admin.register(InstanceSetting)
class InstanceSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at", "updated_by")
    readonly_fields = ("updated_at",)
    exclude = ("value_encrypted",)
