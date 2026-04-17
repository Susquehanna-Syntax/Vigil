from django.contrib import admin

from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "totp_confirmed_at")
    readonly_fields = ("totp_secret", "totp_confirmed_at")
    search_fields = ("user__username",)
