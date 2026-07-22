from django.contrib import admin

from .models import HostSiteAssignment, Site


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_default", "created_at")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(HostSiteAssignment)
class HostSiteAssignmentAdmin(admin.ModelAdmin):
    list_display = ("host", "site", "assigned_at")
    list_filter = ("site",)
