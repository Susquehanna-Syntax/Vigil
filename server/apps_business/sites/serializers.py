from rest_framework import serializers

from .models import HostSiteAssignment, Site


class SiteSerializer(serializers.ModelSerializer):
    host_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Site
        fields = ["id", "name", "slug", "description", "is_global",
                  "host_count", "created_at"]
        # is_global is structural: set once by migration, never over the API.
        # Clients read it to tell the cascading scope apart from a real site.
        read_only_fields = ["id", "created_at", "host_count", "is_global"]


class HostSiteAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = HostSiteAssignment
        fields = ["host", "site", "assigned_at"]
