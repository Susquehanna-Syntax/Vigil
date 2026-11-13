"""OpenAPI shapes of the Store's endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class CatalogDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class CatalogCardSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    source = serializers.CharField()
    source_label = serializers.CharField()
    release = serializers.CharField(allow_blank=True)
    package_id = serializers.CharField()
    name = serializers.CharField()
    summary = serializers.CharField(allow_blank=True)
    publisher = serializers.CharField(allow_blank=True)
    version = serializers.CharField(allow_blank=True)
    platform = serializers.CharField()
    categories = serializers.ListField(child=serializers.CharField())
    icon = serializers.BooleanField()
    installed = serializers.IntegerField(help_text="Hosts that have it")


class CatalogBrowseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    results = CatalogCardSerializer(many=True)
    enabled = serializers.BooleanField(help_text="Whether the catalog sync is on")
    empty = serializers.BooleanField(help_text="Whether the catalog has any package yet")


class CatalogCategorySerializer(serializers.Serializer):
    name = serializers.CharField()
    count = serializers.IntegerField()


class CatalogCategoriesSerializer(serializers.Serializer):
    results = CatalogCategorySerializer(many=True)


class CatalogReleaseSerializer(serializers.Serializer):
    release = serializers.CharField(allow_blank=True)
    version = serializers.CharField(allow_blank=True)


class CatalogAlsoSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    source = serializers.CharField()
    package_id = serializers.CharField()
    platform = serializers.CharField()
    version = serializers.CharField(allow_blank=True)
    source_label = serializers.CharField()
    installed = serializers.IntegerField()


class CatalogPackageDetailSerializer(CatalogCardSerializer):
    description = serializers.CharField(allow_blank=True)
    homepage = serializers.CharField(allow_blank=True)
    license = serializers.CharField(allow_blank=True)
    releases = CatalogReleaseSerializer(many=True)
    also = CatalogAlsoSerializer(many=True)


class CatalogSyncRowSerializer(serializers.Serializer):
    source = serializers.CharField()
    source_label = serializers.CharField()
    release = serializers.CharField(allow_blank=True)
    count = serializers.IntegerField()
    error = serializers.CharField(allow_blank=True)
    synced_at = serializers.DateTimeField(allow_null=True)


class CatalogWantedSerializer(serializers.Serializer):
    source = serializers.CharField()
    source_label = serializers.CharField()
    platform = serializers.CharField()
    release = serializers.CharField(allow_blank=True)


class CatalogStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    locked = serializers.BooleanField(help_text="Set in the environment, not here")
    syncs = CatalogSyncRowSerializer(many=True)
    wanted = CatalogWantedSerializer(many=True)
    packages = serializers.IntegerField()


class CatalogEnableSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(
        required=False, help_text="Defaults to true; false turns the catalog off.")


class CatalogInstallTaskSerializer(serializers.Serializer):
    packages = serializers.ListField(
        child=serializers.IntegerField(), help_text="1 to 10 catalog package ids")


class CatalogInstallTaskCreatedSerializer(serializers.Serializer):
    definition_id = serializers.UUIDField()
    name = serializers.CharField()


class CatalogErrorSerializer(serializers.Serializer):
    """``install_task`` answers its refusals with ``error``, not ``detail``."""

    error = serializers.CharField()
