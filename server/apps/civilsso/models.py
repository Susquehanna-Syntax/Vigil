"""Civil SSO client state: the identity mapping and the cached key.

Civil (the SQSY shared-identity service) is opt-in. This app is inert until
``CIVIL_URL`` is configured. Local username/password login always keeps
working — Civil being down or unconfigured must never lock anyone out.
"""

from django.conf import settings
from django.db import models


class CivilIdentity(models.Model):
    """Maps a Civil user UUID (the JWT ``sub``) to a local user.

    The mapping table — rather than changing the local user PK — is what
    makes Civil adoption reversible and safe on an existing install: local
    accounts keep their IDs, and the same human is recognized across apps
    by their Civil UUID.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="civil_identity",
    )
    civil_id = models.UUIDField(unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.user} ↔ civil:{self.civil_id}"


class CachedCivilKey(models.Model):
    """Civil's JWT public key, fetched once and kept locally.

    Verification is local — no call to Civil in the login hot path after
    the first fetch. Refreshed only explicitly (``manage.py civil_refresh_key``)
    because key rotation is an operator action, not an ambient one.
    """

    public_key_pem = models.TextField()
    fetched_from = models.URLField()
    fetched_at = models.DateTimeField(auto_now=True)

    @classmethod
    def current(cls) -> str:
        row = cls.objects.order_by("-fetched_at").first()
        return row.public_key_pem if row else ""

    @classmethod
    def store(cls, pem: str, source: str) -> None:
        cls.objects.all().delete()
        cls.objects.create(public_key_pem=pem, fetched_from=source)
