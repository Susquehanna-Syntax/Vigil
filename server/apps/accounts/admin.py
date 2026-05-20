from django.contrib import admin

from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "totp_confirmed_at")
    # The TOTP secret is deliberately never surfaced here — it is encrypted at
    # rest and showing it would defeat that. Only enrollment state is shown.
    fields = ("user", "totp_enrolled", "totp_confirmed_at", "last_totp_used_at")
    readonly_fields = ("user", "totp_enrolled", "totp_confirmed_at", "last_totp_used_at")
    search_fields = ("user__username",)

    @admin.display(boolean=True, description="TOTP enrolled")
    def totp_enrolled(self, obj):
        return bool(obj.totp_confirmed_at)
