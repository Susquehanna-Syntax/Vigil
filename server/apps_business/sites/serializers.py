from rest_framework import serializers

from .models import HostSiteAssignment, Site


class SiteSerializer(serializers.ModelSerializer):
    host_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Site
        fields = ["id", "name", "slug", "description", "is_default", "host_count", "created_at"]
        read_only_fields = ["id", "created_at", "host_count"]

    def update(self, instance, validated_data):
        # Cannot change is_default or rename the default site to something that
        # would break the invariant, but renaming *is* allowed per spec.
        if instance.is_default and validated_data.get("is_default", True) is False:
            raise serializers.ValidationError({"is_default": "Cannot remove default flag."})
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class HostSiteAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = HostSiteAssignment
        fields = ["host", "site", "assigned_at"]
