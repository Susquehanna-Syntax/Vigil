"""The link between a Vigil alert and the Jackil ticket it opened.

This table is the whole reason the integration is not a webhook. A webhook
fires again every time the alert re-fires and leaves the helpdesk holding six
tickets for one bad disk; a stored link means the second event finds the first
ticket and posts to it instead.
"""

from __future__ import annotations

from django.db import models


class JackilTicket(models.Model):
    """One alert, one ticket.

    ``OneToOne`` is the constraint doing the work: the database refuses a
    second ticket for the same alert even if two workers race the same event.
    """

    alert = models.OneToOneField(
        "alerts.Alert", on_delete=models.CASCADE, related_name="jackil_ticket")
    ticket_id = models.PositiveIntegerField()

    #: The absolute URL as it was when the ticket was opened. Stored rather
    #: than rebuilt, so moving Jackil later does not silently rewrite history
    #: into links that 404.
    url = models.URLField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    #: Set when Vigil posted the "alert cleared" note, whether or not it also
    #: resolved the ticket. Prevents a flapping alert posting the same note
    #: over and over.
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"jackil#{self.ticket_id} for alert {self.alert_id}"
