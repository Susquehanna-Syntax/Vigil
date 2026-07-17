import uuid

from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    """One audited action: who did what to what, authenticated how.

    Rows are recorded on EVERY install regardless of license (same reasoning
    as migrations always running, §3: empty-cost, instant value on upgrade —
    a customer who buys Business gets history from day one, not from
    purchase day). The license gates *viewing* — proving to a third party
    what happened is the accountability axis Business sells (§0/§1).

    Append-only by convention: nothing in Vigil updates or deletes rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="audit_events",
    )
    # Denormalized so the trail survives user deletion (SET_NULL above).
    username = models.CharField(max_length=150, blank=True, default="")
    action = models.CharField(max_length=100, db_index=True)   # e.g. "host.approved"
    target = models.CharField(max_length=255, blank=True, default="")
    auth_method = models.CharField(max_length=50, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username or 'system'} {self.action} {self.target}"


def record(action: str, *, user=None, target: str = "", auth_method: str = "",
           ip: str | None = None, **detail) -> None:
    """Fire-and-forget audit write. Never raises — an audit failure must not
    break the action being audited."""
    import json
    import logging
    try:
        # Coerce to JSON-safe before touching the DB: an unserializable value
        # must degrade to its str(), not poison the caller's transaction.
        detail = json.loads(json.dumps(detail, default=str))
        AuditEvent.objects.create(
            user=user if getattr(user, "is_authenticated", False) else None,
            username=getattr(user, "username", "") or "",
            action=action,
            target=str(target)[:255],
            auth_method=auth_method,
            ip=ip,
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger("vigil.audits").exception("audit write failed for %s", action)
