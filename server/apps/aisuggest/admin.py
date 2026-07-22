from django.contrib import admin

from .models import AiProvider


@admin.register(AiProvider)
class AiProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "model", "enabled", "order")
    list_editable = ("enabled", "order")
