from django.contrib import admin

from .models import IntegrationKey, JackilTicket, JackilWorkTicket


@admin.register(IntegrationKey)
class IntegrationKeyAdmin(admin.ModelAdmin):
    # key_hash is read-only and never in list_display: the admin is where a
    # careless screenshot gets taken, and a hash is the last thing standing
    # between a leaked image and a working credential.
    list_display = ("name", "prefix", "created_by", "created_at", "last_used_at",
                    "revoked_at")
    readonly_fields = ("key_hash", "created_at", "created_by")
    search_fields = ("name", "prefix")


@admin.register(JackilTicket)
class JackilTicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "alert", "created_at", "cleared_at")
    readonly_fields = ("created_at",)
    search_fields = ("ticket_id",)


@admin.register(JackilWorkTicket)
class JackilWorkTicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "host", "key", "created_at", "closed_at")
    readonly_fields = ("created_at",)
    search_fields = ("ticket_id", "key")
