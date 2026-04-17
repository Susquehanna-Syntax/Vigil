from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    """Per-user profile — currently holds the TOTP enrollment state."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    # base32-encoded TOTP secret (empty until enrollment begins).
    totp_secret = models.CharField(max_length=64, blank=True, default="")
    # Set once the user has verified a code with this secret. Until then the
    # secret is "pending" and TOTP is NOT considered enabled.
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"profile<{self.user_id}>"
