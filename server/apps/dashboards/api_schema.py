"""OpenAPI shapes of the dashboards endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class DashboardWidgetSerializer(serializers.Serializer):
    id = serializers.CharField()
    kind = serializers.CharField()
    x = serializers.IntegerField()
    y = serializers.IntegerField()
    w = serializers.IntegerField()
    h = serializers.IntegerField()
    #: Whatever the widget's kind accepts, as the catalog describes it. Keys
    #: and value types are per-kind data, not a fixed field list.
    settings = serializers.DictField(allow_null=True)


class DashboardSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    is_default = serializers.BooleanField()
    shared = serializers.BooleanField()
    owner = serializers.CharField()
    #: False for someone else's shared dashboard, which reads but does not write.
    is_mine = serializers.BooleanField()
    widgets = DashboardWidgetSerializer(many=True)


class DashboardCreateSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="1-120 characters, unique per owner.")


class DashboardPatchSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_null=True,
                                 help_text="Omit to keep the name; empty is refused.")
    is_default = serializers.BooleanField(
        required=False, help_text="Setting one default clears the previous one.")


class DashboardLayoutWidgetSerializer(serializers.Serializer):
    """One placed tile. An ``id`` that names an existing widget updates it in
    place; without one a new widget is created, and anything the request omits
    is deleted."""

    id = serializers.CharField(required=False, allow_null=True)
    kind = serializers.CharField()
    x = serializers.IntegerField(help_text="Grid column, 0 or more.")
    y = serializers.IntegerField(help_text="Grid row, 0 or more.")
    #: Required: an absent size is refused rather than defaulted to 6.
    w = serializers.IntegerField(help_text="Column span.")
    h = serializers.IntegerField(help_text="Row span, required like ``w``.")
    settings = serializers.DictField(required=False, allow_null=True)


class DashboardLayoutSerializer(serializers.Serializer):
    widgets = DashboardLayoutWidgetSerializer(many=True)


class DashboardShareSerializer(serializers.Serializer):
    shared = serializers.BooleanField()


class DashboardSettingSerializer(serializers.Serializer):
    """One settings field of the catalogue. The shape varies by ``type`` —
    a choice field adds ``options``, an int adds ``min``/``max`` — so this is
    one free-form mapping with the two keys every kind carries."""

    type = serializers.CharField()
    label = serializers.CharField()


class DashboardWidgetSpecSerializer(serializers.Serializer):
    """One catalogue entry: how to place the tile and what it may hold. A
    ``feature`` field rides along on a widget that needs a licence."""

    group = serializers.CharField(help_text="The Add-widget rail it sits in.")
    label = serializers.CharField()
    description = serializers.CharField()
    w = serializers.IntegerField(help_text="Default width, in grid columns.")
    h = serializers.IntegerField(help_text="Default height, in grid rows.")
    min_w = serializers.IntegerField()
    min_h = serializers.IntegerField()
    #: A Jackil tile: served by the helpdesk, not this instance's registry.
    remote = serializers.BooleanField(required=False)
    shape = serializers.CharField(required=False)
    feature = serializers.CharField(required=False)
    settings = serializers.DictField(child=DashboardSettingSerializer())


class DashboardCatalogSerializer(serializers.Serializer):
    #: Catalogue key (the widget kind) to its entry. The keys are data, so
    #: this is one free-form mapping of the entries above.
    widgets = serializers.DictField(child=DashboardWidgetSpecSerializer())
    grid_columns = serializers.IntegerField(
        help_text="Width every layout is expressed against.")


class DashboardUpgradeSerializer(serializers.Serializer):
    """What sharing without a licence answers with: 402."""

    feature = serializers.CharField()
    licensed = serializers.BooleanField()
    detail = serializers.CharField()
    upgrade_url = serializers.URLField()


class DashboardErrorSerializer(serializers.Serializer):
    """What every refusal here says. This app spells the field ``error``."""

    error = serializers.CharField()
