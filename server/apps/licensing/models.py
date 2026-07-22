import uuid

from django.db import models


class InstanceIdentity(models.Model):
    """This deployment's identity — one row, ever.

    Generated at first use and stored in the DB (not the filesystem, not an
    env default) so web, Celery workers, and beat all read the same row and
    replicas share one ID. Survives container replacement and image upgrades;
    dies with the database. Licenses are bound to this UUID (SQSY-LICENSING.md
    §4): a license issued for a different instance reads as *no license*,
    never as an error.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def get(cls) -> "InstanceIdentity":
        row = cls.objects.order_by("created_at").first()
        if row is None:
            row = cls.objects.create()
        return row

    def __str__(self) -> str:
        return str(self.id)


class StoredLicense(models.Model):
    """The license blob pasted through the UI — the lowest-priority source.

    ``VIGIL_LICENSE_KEY`` (env) wins over this row; see
    ``vigil.licensing.load_blob``. One row, replaced on each paste, kept even
    when invalid so the license screen can show *why* it isn't verifying.
    """

    blob = models.TextField()
    added_at = models.DateTimeField(auto_now=True)

    @classmethod
    def current_blob(cls) -> str:
        row = cls.objects.order_by("-added_at").first()
        return row.blob.strip() if row else ""

    @classmethod
    def replace(cls, blob: str) -> None:
        cls.objects.all().delete()
        if blob.strip():
            cls.objects.create(blob=blob.strip())

    def __str__(self) -> str:
        return f"license added {self.added_at:%Y-%m-%d %H:%M}"
