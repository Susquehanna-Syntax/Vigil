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


class InstallKey(models.Model):
    """The key this install signs license refreshes with — one row, ever.

    Kept apart from InstanceIdentity on purpose. Copying the instance id into
    another database (to reuse one license on two installs) does not copy
    this, so Mercantil sees a second install and refuses it (SQSY-LICENSING.md;
    Mercantil licenses/views.py). Stored as is rather than encrypted with
    DJANGO_SECRET_KEY: rotating that secret would otherwise mint a new key and
    lock the customer's own install out of its renewals, and anyone who can
    read this table can copy the whole install anyway.
    """

    seed = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    # The last refresh, for the license screen and for deciding whether the
    # next one is due (vigil.licensing.refresh_license). ``refreshed_lid`` is
    # the license that refresh was about: a newly applied one is claimed at once.
    refreshed_at = models.DateTimeField(null=True, blank=True)
    refresh_result = models.CharField(max_length=20, blank=True, default="")
    refresh_detail = models.CharField(max_length=300, blank=True, default="")
    refreshed_lid = models.CharField(max_length=64, blank=True, default="")

    @classmethod
    def get(cls) -> "InstallKey":
        row = cls.objects.order_by("created_at").first()
        if row is None:
            import base64

            from nacl.signing import SigningKey
            row = cls.objects.create(seed=base64.b64encode(bytes(SigningKey.generate())).decode())
        return row

    def signing_key(self):
        import base64

        from nacl.signing import SigningKey
        return SigningKey(base64.b64decode(self.seed))

    def __str__(self) -> str:
        return f"install key from {self.created_at:%Y-%m-%d}"
