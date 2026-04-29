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


class ADConfig(models.Model):
    """LDAP / Active Directory connection settings for fleet import.

    A singleton in practice — only one AD source per Vigil instance — but
    modelled as a regular row so admins can disable without deleting. The
    bind password is stored using Django's symmetric encryption (Fernet)
    keyed off SECRET_KEY; see ``encrypt_secret`` / ``decrypt_secret``.
    """

    ldap_url = models.CharField(max_length=512, blank=True)
    bind_dn = models.CharField(max_length=512, blank=True)
    bind_password_encrypted = models.BinaryField(blank=True, default=b"")
    base_dn = models.CharField(max_length=512, blank=True)
    computer_ou = models.CharField(max_length=512, blank=True)
    enabled = models.BooleanField(default=False)
    last_sync = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AD: {self.ldap_url or 'unset'}"


class HostInventory(models.Model):
    """Hardware inventory snapshot for a host.

    Populated by the agent on a slow cadence (hourly by default) since
    hardware changes infrequently. ``custom_columns`` is a free-form bag of
    values populated by tasks marked ``collect:`` in their YAML — those
    appear as additional columns on the Inventory page.
    """

    host = models.OneToOneField(Host, on_delete=models.CASCADE, related_name="inventory")
    mac_addresses = models.JSONField(default=dict, blank=True)
    ram_total_bytes = models.BigIntegerField(null=True, blank=True)
    cpu_model = models.CharField(max_length=255, blank=True)
    cpu_cores = models.IntegerField(null=True, blank=True)
    service_tag = models.CharField(max_length=120, blank=True)
    manufacturer = models.CharField(max_length=120, blank=True)
    model_name = models.CharField(max_length=160, blank=True)
    disks = models.JSONField(default=list, blank=True)
    custom_columns = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Inventory: {self.host.hostname}"
