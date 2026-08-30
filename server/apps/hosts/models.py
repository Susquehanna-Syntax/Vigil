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
    #: Row-backed mirror of ``tags``. Populated alongside the strings during
    #: the migration to database-defined tags; the strings stay authoritative
    #: until the switch-over, and a consistency test asserts the two agree.
    tag_rows = models.ManyToManyField("hosts.Tag", blank=True, related_name="hosts")

    #: (string field, row relation) pairs kept in step on save.
    tag_sync_fields = [("tags", "tag_rows")]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Defined below this class, hence the local import.
        for string_field, relation in self.tag_sync_fields:
            sync_tag_rows(self, string_field, relation)
    agent_version = models.CharField(max_length=50, blank=True, default="")
    last_checkin = models.DateTimeField(null=True, blank=True)
    # Alert suppression window. A rebuild takes ~40 minutes, and without this
    # the first one pages everyone and teaches people to ignore the alerts.
    # Set by RebuildJob entering REBOOTING, cleared on every terminal state.
    maintenance_until = models.DateTimeField(null=True, blank=True)
    # Agent-reported "a reboot is pending" (e.g. after installing updates).
    # An absent check-in field means the agent is too old to report it, so
    # the checkin ingest only writes this when the key is present.
    reboot_required = models.BooleanField(default=False)
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
    os_name = models.CharField(max_length=200, blank=True)
    os_version = models.CharField(max_length=120, blank=True)
    kernel_version = models.CharField(max_length=120, blank=True)
    architecture = models.CharField(max_length=32, blank=True)
    uptime_seconds = models.BigIntegerField(null=True, blank=True)
    last_logged_user = models.CharField(max_length=120, blank=True)
    bios_version = models.CharField(max_length=120, blank=True)
    bios_date = models.CharField(max_length=50, blank=True)
    system_timezone = models.CharField(max_length=80, blank=True)
    disks = models.JSONField(default=list, blank=True)
    custom_columns = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Inventory: {self.host.hostname}"


class DockerContainer(models.Model):
    """One Docker container running on a host, from the agent's snapshot.

    Refreshed wholesale on each checkin that carries a ``docker_containers``
    payload: the host's existing rows are replaced, so this table always
    reflects the latest snapshot rather than history.
    """

    host = models.ForeignKey(
        Host, on_delete=models.CASCADE, related_name="docker_containers"
    )
    container_id = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    image = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=20, blank=True)  # running, exited, paused, ...
    status = models.CharField(max_length=120, blank=True)  # "Up 3 hours"
    stack = models.CharField(max_length=200, blank=True)  # compose project
    service = models.CharField(max_length=200, blank=True)  # compose service
    cpu_percent = models.FloatField(null=True, blank=True)
    mem_usage_bytes = models.BigIntegerField(null=True, blank=True)
    mem_limit_bytes = models.BigIntegerField(null=True, blank=True)
    mem_percent = models.FloatField(null=True, blank=True)
    ports = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stack", "name"]
        indexes = [models.Index(fields=["host", "stack"])]

    def __str__(self):
        return f"{self.host.hostname}/{self.name}"


class UnmanagedDevice(models.Model):
    """A device on the network that does NOT run a Vigil agent.

    A plain manual registry — routers, switches, printers, NAS boxes, IoT
    gear — so an operator can keep an inventory of everything on the network,
    not just agent-enrolled hosts. Deliberately manual and static: automated
    discovery / SNMP polling is out of scope for Community.
    """

    class DeviceType(models.TextChoices):
        ROUTER = "router", "Router"
        SWITCH = "switch", "Switch"
        FIREWALL = "firewall", "Firewall"
        ACCESS_POINT = "access_point", "Access Point"
        PRINTER = "printer", "Printer"
        NAS = "nas", "NAS"
        SERVER = "server", "Server"
        WORKSTATION = "workstation", "Workstation"
        CAMERA = "camera", "Camera"
        UPS = "ups", "UPS"
        IOT = "iot", "IoT Device"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    device_type = models.CharField(
        max_length=20, choices=DeviceType.choices, default=DeviceType.OTHER,
    )
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    mac_address = models.CharField(max_length=17, blank=True)
    vendor = models.CharField(max_length=120, blank=True)
    location = models.CharField(max_length=160, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.device_type})"


class HostFirewall(models.Model):
    """The last firewall snapshot read from a host.

    One row per host, overwritten on each read: current state is the point,
    not history. Changes are already recorded as tasks in the audit trail.

    ``enabled`` is tri-state: on Windows a failed profile read reports
    ``None`` (unknown) rather than ``False`` (disabled) -- collapsing an
    unread state to "disabled" would be a confident wrong answer, which is
    worse than admitting the read failed. ``defaults`` values can likewise be
    the string ``"unknown"`` for the same reason, not just "allow"/"deny".
    ``unparsed`` holds anything a backend could not parse (ufw app-profile
    rules, firewalld port ranges, Windows rules with non-integer ports, or a
    whole-payload read failure) so a firewall view can admit what it did not
    understand rather than silently showing fewer rules than the host has.
    """

    host = models.OneToOneField(Host, on_delete=models.CASCADE,
                                related_name="firewall")
    tool = models.CharField(max_length=32, blank=True, default="")
    supported = models.BooleanField(default=True)
    enabled = models.BooleanField(null=True, default=None)
    defaults = models.JSONField(default=dict, blank=True)
    profiles = models.JSONField(default=list, blank=True)
    rules = models.JSONField(default=list, blank=True)
    unparsed = models.JSONField(default=list, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.host.hostname} firewall ({self.tool or 'none'})"


class Tag(models.Model):
    """A tag as a row, rather than a string repeated across every model.

    **Canonicalisation is by lowercasing only — never by stripping.** That is
    not an aesthetic choice, it is what makes the migration from string tags
    behaviour-preserving:

      * ``Prod`` / ``prod`` / ``PROD`` already matched each other, because every
        matcher folded case. They become one row and nothing changes.
      * ``prod`` / ``prod `` never matched, because no matcher stripped. They
        stay two rows and nothing changes.

    If two spellings did not match before, they must not start matching now.
    A trailing space therefore makes a genuinely different tag, exactly as it
    does today — visible and odd, which is better than silently merged.
    """

    class Kind(models.TextChoices):
        MANUAL = "manual", "Set by an operator"
        AUTO = "auto", "Derived from inventory"
        AGENT = "agent", "Advertised by the agent"

    #: Namespaces the server owns. An operator may not create or edit a tag in
    #: these, because the next check-in would overwrite it — and because
    #: ``agent:*`` exists so a compromised agent cannot impersonate an
    #: operator-set tag. Mirrors _AUTO_TAG_PREFIXES / _AGENT_TAG_PREFIX in
    #: apps/hosts/views.py; keep the two in step.
    RESERVED_PREFIXES = ("os:", "os_family:", "pkg:", "arch:", "agent:")

    name = models.CharField(max_length=120)
    #: ``name.lower()``. Whitespace deliberately preserved — see the class
    #: docstring. Unique, so case variants cannot diverge into two rows.
    key = models.CharField(max_length=120, unique=True, db_index=True)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.MANUAL)
    description = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.name

    @staticmethod
    def canonical_key(name) -> str:
        """The comparison key for a tag name. Lowercase; whitespace kept."""
        return str(name).lower()

    @classmethod
    def kind_for(cls, name) -> str:
        lowered = str(name).lower()
        if lowered.startswith("agent:"):
            return cls.Kind.AGENT
        if lowered.startswith(("os:", "os_family:", "pkg:", "arch:")):
            return cls.Kind.AUTO
        return cls.Kind.MANUAL

    @classmethod
    def is_reserved(cls, name) -> bool:
        return str(name).lower().startswith(cls.RESERVED_PREFIXES)

    def save(self, *args, **kwargs):
        self.key = self.canonical_key(self.name)
        if not self.kind or self.kind == self.Kind.MANUAL:
            self.kind = self.kind_for(self.name)
        super().save(*args, **kwargs)

    @classmethod
    def get_or_create_by_name(cls, name):
        """Fetch or create the tag for ``name``, matching on the canonical key.

        Returns ``(tag, created)``. Raises ``ValueError`` for a blank name —
        an empty tag matches nothing and is never what the caller meant.
        """
        if not str(name).strip():
            raise ValueError("a tag needs a name")
        key = cls.canonical_key(name)
        existing = cls.objects.filter(key=key).first()
        if existing:
            return existing, False
        return cls.objects.create(name=str(name), kind=cls.kind_for(name)), True


def sync_tag_rows(instance, string_field: str, relation: str) -> bool:
    """Reconcile a row relation with the string list it mirrors.

    Strings stay the write interface — six different places assign
    ``host.tags`` and it would be a losing game to convert them all — so the
    rows are derived here instead, on save. Returns True when something
    changed.

    Cheap when nothing moved: one query for the current set, and no writes.
    That matters because this runs on every check-in for every host.
    """
    names = [str(n) for n in (getattr(instance, string_field, None) or [])
             if str(n).strip()]
    wanted_keys = {Tag.canonical_key(n) for n in names}
    manager = getattr(instance, relation)
    current = {t.key: t for t in manager.all()}
    if wanted_keys == set(current):
        return False

    tags = []
    for name in names:
        key = Tag.canonical_key(name)
        tag = current.get(key) or Tag.objects.filter(key=key).first()
        if tag is None:
            tag = Tag.objects.create(name=name, kind=Tag.kind_for(name))
        tags.append(tag)
    manager.set(tags)
    return True


class TagRowSyncMixin:
    """Keeps a model's row relation in step with its string field on save.

    ``tag_sync_fields`` is a list of ``(string_field, relation)`` pairs.
    """

    tag_sync_fields: list = []

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        for string_field, relation in self.tag_sync_fields:
            sync_tag_rows(self, string_field, relation)
