"""OpenAPI shapes of the reports endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class ReportDetailSerializer(serializers.Serializer):
    """A refusal. Named for its app: licensing already registers the plain
    ``detail`` body, and two components with one name collide in the schema."""

    detail = serializers.CharField()


# -- a saved report ------------------------------------------------------------

class ReportWidgetSpecSerializer(serializers.Serializer):
    """One entry of a report's ``widgets`` list: the kind, with every drawing
    option and grid place optional — validated by hand, so only ``kind`` is
    documented as present."""

    kind = serializers.CharField()


class ReportRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    range_days = serializers.IntegerField()
    widgets = ReportWidgetSpecSerializer(many=True)
    playbooks = serializers.ListField(child=serializers.CharField())
    automations = serializers.ListField(child=serializers.CharField())
    owner = serializers.CharField(allow_null=True)
    updated_at = serializers.DateTimeField()


class ReportCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True)
    range_days = serializers.IntegerField()
    widgets = ReportWidgetSpecSerializer(many=True)
    playbooks = serializers.ListField(child=serializers.UUIDField(), required=False)
    automations = serializers.ListField(child=serializers.UUIDField(), required=False)


class ReportPatchSerializer(serializers.Serializer):
    """Every field is optional: a PATCH keeps what it does not carry."""

    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    range_days = serializers.IntegerField(required=False)
    widgets = ReportWidgetSpecSerializer(many=True, required=False)
    playbooks = serializers.ListField(child=serializers.UUIDField(), required=False)
    automations = serializers.ListField(child=serializers.UUIDField(), required=False)


# -- the widget catalogue ------------------------------------------------------

class WidgetCatalogSizeSerializer(serializers.Serializer):
    w = serializers.IntegerField()
    h = serializers.IntegerField()


class WidgetCatalogEntrySerializer(serializers.Serializer):
    kind = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    chart = serializers.CharField()
    family = serializers.CharField()
    charts = serializers.ListField(child=serializers.CharField())
    x_label = serializers.CharField()
    y_label = serializers.CharField()
    how = serializers.CharField()
    size = WidgetCatalogSizeSerializer()
    # Chart name to the pair of default axis titles; only axis charts appear.
    labels = serializers.DictField(child=serializers.ListField(
        child=serializers.CharField()))


# -- computed report data ------------------------------------------------------

class WidgetDataSerializer(serializers.DictField):
    """A widget's computed metric. Which keys are present depends on the kind:
    a days chart carries ``days`` with its ``series``, its ``ok``/``failed``/
    ``pending`` buckets or its ``median``/``p95``; a rows chart one ``rows``
    list of per-record dicts. Free-form, because no single fixed shape fits.

    The range beside it is a plain :class:`serializers.DictField` for the same
    reason: its keys are ``from`` and ``to``, which Python will not let a
    serializer declare as fields."""


class ReportWidgetSerializer(serializers.Serializer):
    kind = serializers.CharField()
    title = serializers.CharField()
    chart = serializers.CharField()
    x_label = serializers.CharField()
    y_label = serializers.CharField()
    legend = serializers.BooleanField(allow_null=True)
    layout = serializers.DictField(child=serializers.IntegerField(), allow_null=True)
    sources = serializers.ListField(child=serializers.CharField(), allow_null=True)
    data = WidgetDataSerializer()


class ReportDataSerializer(serializers.Serializer):
    range = serializers.DictField(
        help_text="\"from\", \"to\" and the list of ISO days the widgets cover.")
    truncated = serializers.BooleanField()
    widgets = ReportWidgetSerializer(many=True)


class ReportPreviewSerializer(serializers.Serializer):
    """A preview is the create body with no name and nothing saved."""

    range_days = serializers.IntegerField()
    widgets = ReportWidgetSpecSerializer(many=True)
    playbooks = serializers.ListField(child=serializers.UUIDField(), required=False)
    automations = serializers.ListField(child=serializers.UUIDField(), required=False)
