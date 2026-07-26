from django.contrib import admin

from .models import BrandingConfig

@admin.register(BrandingConfig)
class BrandingConfigAdmin(admin.ModelAdmin):
    list_display = ("pk", "product_name", "accent", "updated_at")
    fieldsets = (
        (None, {"fields": ("product_name", "logo_url", "accent", "footer_text", "support_url")}),
        ("Meta", {"fields": ("updated_at",), "classes": ("collapse",)}),
    )
    readonly_fields = ("updated_at",)
