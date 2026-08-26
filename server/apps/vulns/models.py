import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import Host


class VulnSummary(models.Model):
    """Per-host vulnerability roll-up.

    Counts are de-duplicated across scanners by CVE (or by plugin id when
    the finding has no CVE) so two engines flagging the same vuln don't
    double-penalize the host. The ``score`` is computed from those counts
    with :func:`apps.vulns.scoring.compute_score` — 100 minus a weighted
    deduction, with no floor. A host with 15 criticals lands at ``-50``;
    the negativity is part of the message and the face badge keeps
    escalating with it.
    """

    host = models.OneToOneField(Host, on_delete=models.CASCADE, related_name="vuln_summary")
    last_scan_at = models.DateTimeField(null=True, blank=True)
    scanner_scan_id = models.IntegerField(null=True, blank=True)
    critical = models.IntegerField(default=0)
    high = models.IntegerField(default=0)
    medium = models.IntegerField(default=0)
    low = models.IntegerField(default=0)
    info = models.IntegerField(default=0)
    # Can go negative when deductions exceed 100. No separate "debt"
    # field — that was the floor's overflow indicator before the floor
    # was removed.
    score = models.IntegerField(default=100)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Worst-first via ascending score (negative numbers sort first).
        ordering = ["score", "-critical", "-high"]
        verbose_name = "Vulnerability Summary"
        verbose_name_plural = "Vulnerability Summaries"

    def __str__(self):
        return f"Vulns: {self.host.hostname} (C:{self.critical}/H:{self.high}/M:{self.medium}) score={self.score}"


class VulnScan(models.Model):
    """A request or in-flight scan against a single host.

    Lifecycle::

        REQUESTED → LAUNCHED → RUNNING → COMPLETED   (success)
                                       → FAILED      (scanner errored)
                                       → ABORTED     (scanner or admin canceled)

    Created either by the "Scan now" UI (with ``requested_by`` set) or by
    the ``request_nessus_scan`` task action (``requested_by`` = None,
    ``requested_via_task`` = True).

    The ``scanner`` field records which engine in
    :data:`apps.vulns.scanners.SCANNER_REGISTRY` owns this row — that
    impl is the one whose ``sync()`` cycle will launch, poll, and ingest
    this scan. ``external_scan_id`` stores the scanner-side identifier
    (Nessus's integer scan id, Greenbone's task UUID, etc.) as a string
    so it's engine-agnostic.
    """

    class Scanner(models.TextChoices):
        NESSUS = "nessus", "Nessus"
        GREENBONE = "greenbone", "Greenbone / OpenVAS"
        TRIVY = "trivy", "Trivy"

    class State(models.TextChoices):
        REQUESTED = "requested", "Requested"
        LAUNCHED = "launched", "Launched"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        ABORTED = "aborted", "Aborted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="vuln_scans")
    scanner = models.CharField(
        max_length=16, choices=Scanner.choices, default=Scanner.NESSUS,
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.REQUESTED)
    external_scan_id = models.CharField(max_length=64, blank=True, default="")
    target = models.CharField(max_length=255, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    launched_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # When this scan's results were pulled into VulnFinding rows. The
    # sync only ingests COMPLETED scans whose ingested_at is unset, so
    # each scan is processed exactly once — re-ingesting an old scan
    # after a newer one would wrongly mark the newer findings FIXED.
    ingested_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="vuln_scans_requested",
    )
    # True when the scan request came from an agent running the
    # request_nessus_scan task action (no human requester).
    requested_via_task = models.BooleanField(default=False)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["host", "-requested_at"]),
            models.Index(fields=["state"]),
            models.Index(fields=["scanner", "state"]),
        ]

    @property
    def is_active(self) -> bool:
        return self.state in {self.State.REQUESTED, self.State.LAUNCHED, self.State.RUNNING}

    def __str__(self):
        return f"VulnScan[{self.state}] {self.host.hostname}"


class VulnFinding(models.Model):
    """One vulnerability finding reported by one scanner on one host.

    Granularity matches what the scanner emits:

      * Nessus: one row per (host, plugin_id). ``cve_id`` may stay empty
        because Nessus's per-host endpoint only surfaces the plugin
        identifier — CVE numbers live behind a separate per-plugin call
        we don't currently make.
      * Trivy: one row per (host, package, CVE) — both ``cve_id`` and
        ``package_name``/``installed_version``/``fixed_version`` are set.
      * Greenbone: one row per (host, OID). ``plugin_id_or_oid`` holds
        the NVT OID, ``cve_id`` is set when the NVT carries one.

    Findings disappear when the next sync no longer reports them; we
    mark them ``FIXED`` rather than deleting, so trend lines and
    "first seen" dates survive across the lifetime of the install.
    """

    class State(models.TextChoices):
        OPEN = "open", "Open"
        FIXED = "fixed", "Fixed"
        SUPPRESSED = "suppressed", "Suppressed"

    class Severity(models.TextChoices):
        CRITICAL = "critical", "Critical"
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"
        INFO = "info", "Informational"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="vuln_findings")
    scanner = models.CharField(max_length=16, choices=VulnScan.Scanner.choices)

    # Engine-specific finding identifier. Nessus = stringified plugin_id,
    # Greenbone = NVT OID, Trivy = CVE id (since Trivy's findings ARE
    # CVE-centric — both fields will hold the same value there).
    plugin_id_or_oid = models.CharField(max_length=128)
    cve_id = models.CharField(max_length=32, blank=True, default="")

    title = models.CharField(max_length=255, blank=True, default="")
    severity = models.CharField(max_length=16, choices=Severity.choices)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)

    # Populated when the scanner reports package-level detail (Trivy,
    # credentialed Greenbone). Stays empty for network-scan plugin
    # findings that don't map to a single OS package.
    package_name = models.CharField(max_length=255, blank=True, default="")
    installed_version = models.CharField(max_length=80, blank=True, default="")
    fixed_version = models.CharField(max_length=80, blank=True, default="")

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # When this must be remediated by. Derived once, in save(), from the active
    # RemediationPolicy and KEV membership — never at the scanner call sites,
    # because there are three of them and there will be more.
    due_date = models.DateField(null=True, blank=True, db_index=True)

    class Meta:
        # Same plugin from the same scanner is the natural finding
        # identity, regardless of whether the scanner has surfaced a CVE.
        # We don't include CVE here because Nessus plugin findings
        # frequently carry no CVE and we don't want one duplicate row
        # per re-sync just because the cve_id field stayed empty.
        constraints = [
            models.UniqueConstraint(
                fields=["host", "scanner", "plugin_id_or_oid"],
                name="vuln_finding_unique_per_host_scanner_plugin",
            ),
        ]
        indexes = [
            models.Index(fields=["host", "state", "severity"]),
            models.Index(fields=["scanner", "state"]),
            models.Index(fields=["cve_id"]),  # dedup-by-CVE recompute query
        ]
        # NOT ordered by severity: it's a TextChoices column, and string
        # order puts "medium" above "critical". Worst-first ordering is
        # done with a SEVERITY_RANK annotation where it's needed (see
        # finding_list).
        ordering = ["-last_seen"]

    def __str__(self):
        ref = self.cve_id or self.plugin_id_or_oid
        return f"{self.severity} {ref} on {self.host.hostname} ({self.scanner})"

    def save(self, *args, **kwargs):
        """Derive ``due_date`` on first write, then leave it alone.

        Setting it only when it is ``None`` is what makes a deadline stable
        across re-scans: every scanner writes through ``update_or_create``,
        which calls save() on every sync, and a deadline that slid forward each
        time a scanner re-reported the finding would never be overdue.
        """
        if self.due_date is None and self.severity != self.Severity.INFO:
            from .remediation import compute_due_date

            kev_entry = None
            if self.cve_id:
                kev_entry = KevEntry.objects.filter(
                    cve_id=self.cve_id.strip().upper()
                ).first()
            self.due_date = compute_due_date(self, kev_entry=kev_entry)
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                kwargs["update_fields"] = list(kwargs["update_fields"]) + ["due_date"]
        super().save(*args, **kwargs)

    @property
    def active_exception(self):
        """The exception suppressing this finding, if one is in force today."""
        exception = getattr(self, "exception", None)
        if exception is not None and exception.is_active:
            return exception
        return None

    @property
    def is_excepted(self) -> bool:
        """True while a documented exception is in force.

        An expired exception simply stops matching — no sweep job needed, and
        no window where a lapsed acceptance is still quietly suppressing a
        finding.
        """
        return self.active_exception is not None

    @property
    def days_remaining(self) -> int | None:
        """Days until the due date. Negative once overdue, ``None`` if undated."""
        if self.due_date is None:
            return None
        from django.utils.timezone import localdate

        return (self.due_date - localdate()).days

    @property
    def overdue(self) -> bool:
        """Past its due date, still open, and not excepted."""
        if self.due_date is None or self.state != self.State.OPEN:
            return False
        if self.is_excepted:
            return False
        from django.utils.timezone import localdate

        return self.due_date < localdate()


class VulnScoreHistory(models.Model):
    """One row per host per day, snapshotted by the daily Celery beat.

    Powers the score sparkline + trend arrow on host detail and the fleet
    Vulnerabilities tab. Pruning is left to a future maintenance task —
    one row per host per day is small enough that we don't need an
    aggressive retention policy.
    """

    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="vuln_score_history")
    date = models.DateField()
    score = models.IntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["host", "date"],
                name="vuln_score_history_unique_per_host_per_day",
            ),
        ]
        indexes = [
            models.Index(fields=["host", "-date"]),
        ]
        ordering = ["-date"]

    def __str__(self):
        return f"{self.host.hostname} {self.date}: score={self.score}"


class RemediationPolicy(models.Model):
    """How long a finding may stay open before it is overdue.

    One row, always. Timelines default to BOD 22-01 shapes but nothing else in
    the codebase hardcodes them — every consumer reads this row, so an operator
    can retune the whole fleet's deadlines from one screen.
    """

    kev_days = models.PositiveIntegerField(default=14)
    critical_days = models.PositiveIntegerField(default=15)
    high_days = models.PositiveIntegerField(default=30)
    medium_days = models.PositiveIntegerField(default=90)
    low_days = models.PositiveIntegerField(default=180)

    #: When a CVE is in the KEV catalogue AND CISA published its own deadline,
    #: use that date instead of ``date_added + kev_days``. Off falls back to the
    #: policy tier, which is what an operator wants if they consider CISA's
    #: dates too aggressive for their environment.
    use_cisa_due_date = models.BooleanField(default=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "remediation policies"

    def __str__(self):
        return (
            f"Remediation policy (KEV {self.kev_days}d, crit {self.critical_days}d, "
            f"high {self.high_days}d)"
        )

    @classmethod
    def get_active(cls) -> "RemediationPolicy":
        """The single policy row, created with defaults on first access."""
        policy = cls.objects.order_by("pk").first()
        if policy is None:
            policy = cls.objects.create()
        return policy


class KevEntry(models.Model):
    """One CVE from the CISA Known Exploited Vulnerabilities catalogue.

    Synced data, not operator-editable. Populated from the snapshot bundled at
    ``apps/vulns/data/kev_catalog.json`` and optionally refreshed from CISA.
    """

    class Source(models.TextChoices):
        BUNDLED = "bundled", "Bundled snapshot"
        LIVE = "live", "Fetched from CISA"

    cve_id = models.CharField(max_length=32, unique=True, db_index=True)
    date_added = models.DateField()
    cisa_due_date = models.DateField(null=True, blank=True)
    ransomware = models.BooleanField(default=False)
    name = models.CharField(max_length=255, blank=True, default="")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.BUNDLED)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_added"]
        verbose_name_plural = "KEV entries"

    def __str__(self):
        return f"{self.cve_id} (KEV, added {self.date_added})"


class VulnException(models.Model):
    """A documented decision to not fix a finding by its due date.

    Excluded from overdue counts and from score escalation, but never hidden:
    an exception is a reporting line, not a delete button. Every one carries a
    written reason and an expiry — there is deliberately no permanent
    exception, because a risk accepted forever is a risk nobody re-examines.
    """

    class Kind(models.TextChoices):
        ACCEPTED = "accepted", "Accepted risk"
        DEFERRED = "deferred", "Deferred"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding = models.OneToOneField(
        "VulnFinding", on_delete=models.CASCADE, related_name="exception"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ACCEPTED)
    reason = models.TextField()
    expires_on = models.DateField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="vuln_exceptions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        from django.core.exceptions import ValidationError

        # An exception without a reason is not an exception, it is a silence.
        if not (self.reason or "").strip():
            raise ValidationError({"reason": "A reason is required."})

    def __str__(self):
        return f"{self.get_kind_display()} until {self.expires_on}"

    @property
    def is_active(self) -> bool:
        from django.utils.timezone import localdate

        return self.expires_on >= localdate()
