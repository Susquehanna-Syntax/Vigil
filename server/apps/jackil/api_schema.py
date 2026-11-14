"""OpenAPI shapes of the Jackil integration endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers

# ── Vigil's widget catalogue, as a helpdesk reads it ────────────────────────


class JackilWidgetSettingSerializer(serializers.Serializer):
    """One settings field of one widget. The shape varies by ``type`` — a
    ``choice`` adds ``choices`` (``[value, label]`` pairs) and a default, an
    ``int`` adds ``min``, ``max`` and a default — so this is one free-form
    mapping with the two keys every kind carries."""

    type = serializers.CharField()
    label = serializers.CharField()


class JackilWidgetRowSerializer(serializers.Serializer):
    """One widget of the catalogue: what to call it, how to draw it, and what
    it will accept on its own URL."""

    kind = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    shape = serializers.CharField(help_text="gauge, stat or list.")
    settings = serializers.DictField(child=JackilWidgetSettingSerializer())


class JackilWidgetCatalogueSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    widgets = JackilWidgetRowSerializer(many=True)


# ── one widget's envelope: the contract a helpdesk draws from ───────────────


class JackilWidgetGaugeDataSerializer(serializers.Serializer):
    """``value`` is null when no host reports in: there is nothing to measure,
    and 0% would read as an outage. ``sub`` rides along with a denominator."""

    value = serializers.FloatField(allow_null=True, help_text="A percentage, 0 to 100.")
    label = serializers.CharField()
    sub = serializers.CharField(required=False)
    tone = serializers.CharField(help_text="ok, warn, bad, info or muted.")


class JackilWidgetStatDataSerializer(serializers.Serializer):
    """``sub`` and ``href`` appear only when there is a second line to show or
    something for the tile to open."""

    value = serializers.IntegerField()
    label = serializers.CharField()
    sub = serializers.CharField(required=False)
    href = serializers.CharField(required=False)
    tone = serializers.CharField(help_text="ok, warn, bad, info or muted.")


class JackilWidgetItemSerializer(serializers.Serializer):
    """One list row. An optional part is present with real content or absent —
    never an empty string standing in for "nothing to show"."""

    title = serializers.CharField()
    meta = serializers.CharField(required=False)
    badge = serializers.CharField(required=False)
    href = serializers.CharField(required=False)
    tone = serializers.CharField(required=False, help_text="ok, warn, bad, info or muted.")


class JackilWidgetListDataSerializer(serializers.Serializer):
    items = JackilWidgetItemSerializer(many=True)
    empty = serializers.CharField(help_text="What to draw when ``items`` is empty.")


class JackilWidgetEnvelopeSerializer(serializers.Serializer):
    """One widget's envelope. One endpoint answers with three bodies and an
    OpenAPI response object is keyed by status code alone, so this is the 200:
    ``shape`` names the key set ``data`` follows — ``JackilWidgetGaugeData``,
    ``JackilWidgetStatData`` or ``JackilWidgetListDataSerializer``, each also
    documented beside it."""

    version = serializers.IntegerField()
    shape = serializers.CharField(help_text="gauge, stat or list: the key set data follows.")
    data = JackilWidgetGaugeDataSerializer()


class JackilWidgetGaugeSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    shape = serializers.CharField()
    data = JackilWidgetGaugeDataSerializer()


class JackilWidgetStatSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    shape = serializers.CharField()
    data = JackilWidgetStatDataSerializer()


class JackilWidgetListSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    shape = serializers.CharField()
    data = JackilWidgetListDataSerializer()


# ── refusals ────────────────────────────────────────────────────────────────


class JackilErrorSerializer(serializers.Serializer):
    """A refusal here, in either of the two wordings this app's front end
    reads (``vigil-utils.js`` takes ``detail``, then ``error``): a view that
    hands back a reason spells it one way and DRF's permission gate the other.
    A body carries one of them, so each is optional."""

    error = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)


class JackilWidgetUnknownSerializer(serializers.Serializer):
    """``widget``: a kind nobody offered, or a setting this widget cannot use."""

    error = serializers.CharField()


# ── the drawer's "Related in Jackil" ────────────────────────────────────────


class JackilRelatedLinkSerializer(serializers.Serializer):
    """One link, cleaned: a URL outside http(s) and a tone outside the
    drawer's stylesheet both come back empty rather than as sent."""

    relation = serializers.CharField()
    label = serializers.CharField()
    url = serializers.CharField(help_text="Empty unless the helpdesk sent an http(s) URL.")
    status = serializers.CharField()
    tone = serializers.CharField(
        help_text="ok, warn, bad, info or muted; empty for anything else."
    )


class JackilRelatedItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    url = serializers.CharField(help_text="Empty unless the helpdesk sent an http(s) URL.")


class JackilRelatedUnconfiguredSerializer(serializers.Serializer):
    """Jackil is switched off or unconfigured, so nothing was asked of it."""

    configured = serializers.BooleanField(help_text="Always false here.")
    links = JackilRelatedLinkSerializer(many=True, help_text="Empty.")
    items = JackilRelatedItemSerializer(many=True, help_text="Empty.")


class JackilRelatedUnavailableSerializer(serializers.Serializer):
    """Jackil was asked and could not answer — down, or too old to offer
    links. The drawer shows ``message`` and stays standing."""

    configured = serializers.BooleanField(help_text="Always true here.")
    available = serializers.BooleanField(help_text="Always false here.")
    message = serializers.CharField()
    links = JackilRelatedLinkSerializer(many=True, help_text="Empty.")
    items = JackilRelatedItemSerializer(many=True, help_text="Empty.")


class JackilRelatedSerializer(serializers.Serializer):
    configured = serializers.BooleanField(help_text="Always true here.")
    available = serializers.BooleanField(help_text="Always true here.")
    links = JackilRelatedLinkSerializer(many=True)
    items = JackilRelatedItemSerializer(many=True)


# ── Jackil's own tiles on a Vigil dashboard ─────────────────────────────────


class JackilRemoteWidgetSerializer(serializers.Serializer):
    """One ``jackil:<kind>`` tile, as the helpdesk drew it.

    The far end is someone else's server: the envelope is passed through
    untouched, so ``data`` is that helpdesk's payload — one of the five shapes
    Vigil knows how to draw. A tile that could not load answers 200 with a
    fallback stat whose ``label`` says why."""

    version = serializers.IntegerField()
    shape = serializers.CharField(help_text="stat, list, table, gauge or series.")
    data = serializers.DictField()


# ── the settings pane: tickets and keys ─────────────────────────────────────


class JackilTicketRowSerializer(serializers.Serializer):
    ticket_id = serializers.IntegerField()
    url = serializers.CharField(help_text="The ticket's own URL in Jackil; empty when none was stored.")
    host = serializers.CharField()
    severity = serializers.CharField()
    message = serializers.CharField()
    created_at = serializers.DateTimeField()
    cleared_at = serializers.DateTimeField(allow_null=True)


class JackilTicketsSerializer(serializers.Serializer):
    count = serializers.IntegerField(help_text="Every visible ticket, not just the five rows.")
    tickets = JackilTicketRowSerializer(many=True)


class JackilKeySerializer(serializers.Serializer):
    """A key as the admin list shows it. Never ``key_hash``, and never the
    plaintext — that is in the mint response alone."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    prefix = serializers.CharField(help_text="The first 8 characters after vjk_.")
    created_by = serializers.CharField()
    created_at = serializers.DateTimeField()
    last_used_at = serializers.DateTimeField(allow_null=True)
    revoked_at = serializers.DateTimeField(allow_null=True)
    can_write_devices = serializers.BooleanField()


class JackilKeyListSerializer(serializers.Serializer):
    keys = JackilKeySerializer(many=True)


class JackilMintedKeySerializer(JackilKeySerializer):
    """The mint response: the one answer that carries ``key``."""

    key = serializers.CharField(help_text="The plaintext. Shown once and never again.")


class JackilKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="What this key is for, 1 to 80 characters.")


class JackilKeyDeviceWriteSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(
        help_text="Whether this key may create and change unmanaged devices. Any other value is refused."
    )


# ── the read surface: search, resolve, detail ───────────────────────────────


class JackilItemSerializer(serializers.Serializer):
    """One row as the search page draws it, for every kind a key may read.

    The same seven keys whatever the kind is: ``status`` and ``tone`` are the
    badge, and both are "" for kinds that have nothing to badge (a playbook, a
    device with no type). ``url`` opens the row in the SPA."""

    type = serializers.CharField(help_text="host, task, playbook, automation, run or device.")
    id = serializers.CharField()
    label = serializers.CharField()
    sub = serializers.CharField(help_text="The second line; empty when there is nothing to add.")
    status = serializers.CharField(help_text="The badge text; empty when this kind has no badge.")
    tone = serializers.CharField(
        help_text="ok, warn, bad, info or muted; empty for a badge with no colour."
    )
    url = serializers.URLField(help_text="Opens the row in the SPA.")


class JackilSearchResultsSerializer(serializers.Serializer):
    results = JackilItemSerializer(many=True)


class JackilResolveItemSerializer(JackilItemSerializer):
    """One resolved row: the search shape, whose ``type`` is the query's and
    therefore the same on every row of one call."""


class JackilResolveSerializer(serializers.Serializer):
    """The rows that exist and are the key creator's to see, keyed by id. An
    id that is malformed, absent or hidden is simply not a key."""

    items = serializers.DictField(child=JackilResolveItemSerializer())


class JackilFactSerializer(serializers.Serializer):
    label = serializers.CharField()
    value = serializers.CharField()


class JackilCpuPointSerializer(serializers.Serializer):
    t = serializers.DateTimeField()
    v = serializers.FloatField(help_text="CPU usage percent, one decimal.")


class JackilDetailSerializer(JackilItemSerializer):
    """Every kind but a host: the row plus its fact rows."""

    facts = JackilFactSerializer(many=True)


class JackilHostDetailSerializer(JackilDetailSerializer):
    """A host's detail: the row, its fact rows, and the sparkline that only a
    host carries."""

    cpu_series = JackilCpuPointSerializer(many=True)


class JackilDeviceWriteSerializer(serializers.Serializer):
    """The device fields ``create_device`` reads from the body:
    ``device_detail_write`` takes any subset of them, which is what a partial
    update means."""

    name = serializers.CharField(help_text="1 to 200 characters, not blank.")
    device_type = serializers.ChoiceField(
        choices=["router", "switch", "firewall", "access_point", "printer", "nas",
                 "server", "workstation", "camera", "ups", "iot", "other"])
    ip_address = serializers.IPAddressField(required=False, allow_null=True)
    mac_address = serializers.CharField(required=False, allow_blank=True)
    vendor = serializers.CharField(required=False, allow_blank=True)
    location = serializers.CharField(required=False, allow_blank=True)
    serial = serializers.CharField(required=False, allow_blank=True)
    warranty_end = serializers.DateField(required=False, allow_null=True)
    owner = serializers.CharField(required=False, allow_blank=True,
                                  help_text="A name or an email address.")
    extra = serializers.DictField(
        required=False, child=serializers.CharField(),
        help_text="Up to 50 free-form facts, names and values both as text.")
    notes = serializers.CharField(required=False, allow_blank=True)
    site_id = serializers.UUIDField(
        required=False, allow_null=True,
        help_text="Ignored: the device goes to the site the key may write in — "
                  "global unless a scoped Business key says otherwise.")


class JackilDeviceDetailSerializer(JackilDetailSerializer):
    """A created, patched or read device: the row and its facts, the same
    shape whichever way it arrived."""


class JackilPingSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    version = serializers.CharField(help_text="This Vigil's version.")
    capabilities = serializers.ListField(
        child=serializers.CharField(),
        help_text='What this caller may do: "devices" and "host_facts", plus '
                  '"device_write" for a key its admin gave the device-write flag.')
    acting_as = serializers.CharField(help_text="The key creator's username.")


class JackilLicenceSerializer(serializers.Serializer):
    blob = serializers.CharField(
        help_text="The signed license token, for the caller to verify with its "
                  'own copy of the public key. "" when this install has none.')
    instance = serializers.CharField(help_text="This deployment's id.")
    status = serializers.CharField(
        help_text="none, valid, expiring, expired or invalid.")


