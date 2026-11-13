"""OpenAPI shapes of the status page endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class StatusPageUptimeBarSerializer(serializers.Serializer):
    """One day of the uptime strip. ``pct`` is null for a day with no samples."""

    pct = serializers.FloatField(allow_null=True,
                                 help_text="Fraction up that day, 0 to 1.")
    state = serializers.CharField(
        help_text='"up", "degraded", "down", or "nodata".')
    label = serializers.CharField(help_text='Tooltip text, e.g. "Nov 3: 99.8% up".')


class StatusPageHostSerializer(serializers.Serializer):
    """One host row of a page. ``id`` is a per-page opaque key, not the host's."""

    id = serializers.CharField()
    hostname = serializers.CharField()
    up = serializers.BooleanField()
    bars = StatusPageUptimeBarSerializer(many=True)
    uptime_pct = serializers.CharField(
        allow_null=True, help_text='Percent up over the window, e.g. "99.87".')


class StatusPageUpgradeSerializer(serializers.Serializer):
    """What a licence limit here answers with: 402 everywhere it refuses."""

    feature = serializers.CharField()
    licensed = serializers.BooleanField()
    detail = serializers.CharField()
    upgrade_url = serializers.URLField()


class StatusPageSerializer(serializers.Serializer):
    id = serializers.CharField()
    #: The public page's unguessable slug, rotatable with ``rotate_token``.
    token = serializers.CharField()
    title = serializers.CharField()
    enabled = serializers.BooleanField()
    #: Host ids as strings; empty means every non-pending host.
    host_ids = serializers.ListField(child=serializers.CharField())
    #: Display-name overrides, host id to public name. The keys are data.
    host_labels = serializers.DictField(child=serializers.CharField())
    site_id = serializers.CharField(allow_null=True)
    url = serializers.CharField(help_text="Where the public page lives.")
    is_primary = serializers.BooleanField()
    logo_url = serializers.CharField(required=False)
    hide_badge = serializers.BooleanField(required=False)


class StatusPageWriteSerializer(serializers.Serializer):
    """POST/PATCH body. Every field is optional; absent ones keep their value."""

    title = serializers.CharField(required=False)
    enabled = serializers.BooleanField(required=False)
    host_ids = serializers.ListField(
        child=serializers.CharField(), required=False, allow_null=True,
        help_text="Host ids the page may publish, all of them visible to the "
                  "caller and in the page's site. Empty means every non-pending "
                  "host of the page's site.")
    host_labels = serializers.DictField(child=serializers.CharField(),
                                        required=False, allow_null=True)
    rotate_token = serializers.BooleanField(
        required=False, help_text="Any true value mints a new page token.")
    # Branding fields: the first status page is free; these need Business, and
    # so does any second page.
    site_id = serializers.CharField(required=False, allow_null=True)
    logo_url = serializers.CharField(required=False)
    hide_badge = serializers.BooleanField(required=False)


class StatusPageDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class StatusPageErrorSerializer(serializers.Serializer):
    """What the refuse-with-a-reason paths here say. This app spells it ``error``."""

    error = serializers.CharField()


class StatusPageHostChoiceSerializer(serializers.Serializer):
    id = serializers.CharField()
    hostname = serializers.CharField()
    up = serializers.BooleanField()


class StatusPageUptimeDaySerializer(serializers.Serializer):
    """One day of one host's uptime, for the dashboard widget."""

    day = serializers.DateField()
    uptime = serializers.FloatField(help_text="Percent up, one decimal.")
