from django.conf import settings
from django.db import models

from apps.hosts.crypto import decrypt_secret, encrypt_secret


class UserProfile(models.Model):
    """Per-user profile — currently holds the TOTP enrollment state."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    # TOTP secret encrypted at rest with Fernet (see apps/hosts/crypto.py) so a
    # database dump does not expose every admin's 2FA seed. Empty until
    # enrollment begins. Read and written through the ``totp_secret`` property,
    # which transparently decrypts on read and encrypts on write.
    totp_secret_encrypted = models.BinaryField(blank=True, default=b"")
    # Set once the user has verified a code with this secret. Until then the
    # secret is "pending" and TOTP is NOT considered enabled.
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Replay-protection: the most recent TOTP code consumed by this user and
    # when it was consumed. Reused codes within the validity window are
    # rejected to prevent an intercepted code from being replayed.
    last_totp_code = models.CharField(max_length=12, blank=True, default="")
    last_totp_used_at = models.DateTimeField(null=True, blank=True)

    @property
    def totp_secret(self) -> str:
        """The base32 TOTP secret in plaintext, decrypted on access."""
        return decrypt_secret(self.totp_secret_encrypted)

    @totp_secret.setter
    def totp_secret(self, value: str) -> None:
        self.totp_secret_encrypted = encrypt_secret(value or "")

    def __str__(self) -> str:
        return f"profile<{self.user_id}>"


class LoginAttempt(models.Model):
    """A failed console login, kept briefly to rate-limit brute forcing.

    Rows are recorded only for failures, cleared for a username on its next
    successful login, and pruned after 24 hours. DB-backed (rather than a
    per-process cache) so the limit holds across gunicorn workers and
    restarts.
    """

    username = models.CharField(max_length=150, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:
        return f"failed login {self.username!r} from {self.ip} at {self.created_at}"
