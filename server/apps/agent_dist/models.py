import uuid

from django.db import models


class AgentBinary(models.Model):
    class Platform(models.TextChoices):
        LINUX_AMD64 = "linux-amd64", "Linux (x86-64)"
        LINUX_ARM64 = "linux-arm64", "Linux (ARM64)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform = models.CharField(max_length=30, choices=Platform.choices, unique=True)
    version = models.CharField(max_length=50, blank=True)
    binary = models.FileField(upload_to="agent_binaries/")
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["platform"]
        verbose_name = "Agent binary"
        verbose_name_plural = "Agent binaries"

    def __str__(self):
        return f"{self.platform} v{self.version or '?'}"
