from rest_framework import serializers

from .models import Task, TaskDefinition, TaskRun


class TaskSerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "host",
            "host_hostname",
            "requested_by",
            "requested_by_username",
            "run",
            "step_order",
            "step_label",
            "action",
            "params",
            "risk_level",
            "state",
            "nonce",
            "ttl_seconds",
            "result_output",
            "created_at",
            "dispatched_at",
            "completed_at",
        ]
        read_only_fields = fields


class TaskDefinitionSerializer(serializers.ModelSerializer):
    owner_username = serializers.CharField(
        source="owner.username", read_only=True, default=None
    )
    action_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TaskDefinition
        fields = [
            "id",
            "owner",
            "owner_username",
            "name",
            "description",
            "relevance",
            "risk_level",
            "visibility",
            "yaml_source",
            "parsed_spec",
            "action_count",
            "forked_from",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "owner_username",
            "parsed_spec",
            "risk_level",
            "visibility",
            "action_count",
            "forked_from",
            "created_at",
            "updated_at",
        ]


class TaskRunSerializer(serializers.ModelSerializer):
    definition_name = serializers.CharField(
        source="definition.name", read_only=True, default=None
    )
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )
    tasks = TaskSerializer(many=True, read_only=True)

    class Meta:
        model = TaskRun
        fields = [
            "id",
            "definition",
            "definition_name",
            "name_snapshot",
            "requested_by",
            "requested_by_username",
            "host_count",
            "step_count",
            "state",
            "created_at",
            "finished_at",
            "tasks",
        ]
        read_only_fields = fields
