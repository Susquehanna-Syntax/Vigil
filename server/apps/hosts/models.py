import uuid

from django.db import models


class Host(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending Enrollment"
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        REJECTED = "rejected", "Rejected"

    class Mode(models.TextChoices):
        MONITOR = "monitor", "Monitor"
        MANAGED = "managed", "Managed"
        FULL_CONTROL = "full_control", "Full Control"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hostname = models.CharField(max_length=255)
    os = models.CharField(max_length=100, blank=True)
    kernel = models.CharField(max_length=100, blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    agent_token = models.CharField(max_length=255, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.MONITOR)
    tags = models.JSONField(default=list, blank=True)
    last_checkin = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["hostname"]

    def __str__(self):
        return f"{self.hostname} ({self.status})"
