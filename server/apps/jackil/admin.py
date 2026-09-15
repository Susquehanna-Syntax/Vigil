from django.contrib import admin

from .models import JackilTicket


@admin.register(JackilTicket)
class JackilTicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "alert", "created_at", "cleared_at")
    readonly_fields = ("created_at",)
    search_fields = ("ticket_id",)
