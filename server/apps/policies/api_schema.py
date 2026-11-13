"""OpenAPI shapes of the policy endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class PolicyAppRuleSerializer(serializers.Serializer):
    app = serializers.CharField()
    source = serializers.CharField(allow_blank=True)
    state = serializers.CharField()
    version = serializers.CharField(allow_blank=True)


class PolicySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    enabled = serializers.BooleanField()
    target_tags = serializers.ListField(child=serializers.CharField())
    site_id = serializers.CharField(allow_null=True)
    cron_minute = serializers.CharField()
    cron_hour = serializers.CharField()
    cron_dow = serializers.CharField()
    window_hours = serializers.IntegerField()
    wave_group_tag = serializers.CharField(allow_blank=True)
    approval_mode = serializers.CharField()
    patch_enabled = serializers.BooleanField()
    windows_classifications = serializers.ListField(child=serializers.CharField())
    deferral_days = serializers.IntegerField()
    reboot = serializers.CharField()
    linux_updates = serializers.CharField()
    allow_high_risk = serializers.BooleanField()
    high_risk = serializers.BooleanField()
    task_definition = serializers.CharField(allow_null=True)
    app_rules = PolicyAppRuleSerializer(many=True)
    created_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)


class PolicyListSerializer(serializers.Serializer):
    results = PolicySerializer(many=True)


class PolicyWriteSerializer(serializers.Serializer):
    """The body fields ``_save`` applies to the policy; anything else in the
    payload is ignored, so the write serializer is not a validator here."""

    name = serializers.CharField(required=False)
    enabled = serializers.BooleanField(required=False)
    target_tags = serializers.ListField(child=serializers.CharField(), required=False)
    site_id = serializers.CharField(required=False, allow_null=True)
    cron_minute = serializers.CharField(required=False)
    cron_hour = serializers.CharField(required=False)
    cron_dow = serializers.CharField(required=False)
    window_hours = serializers.IntegerField(required=False)
    wave_group_tag = serializers.CharField(required=False)
    approval_mode = serializers.CharField(required=False)
    patch_enabled = serializers.BooleanField(required=False)
    windows_classifications = serializers.ListField(child=serializers.CharField(), required=False)
    deferral_days = serializers.IntegerField(required=False)
    reboot = serializers.CharField(required=False)
    linux_updates = serializers.CharField(required=False)
    allow_high_risk = serializers.BooleanField(required=False)
    app_rules = serializers.ListField(child=serializers.DictField(), required=False)
    totp = serializers.CharField(required=False, write_only=True,
                                 help_text="Current TOTP code — only needed to turn "
                                           "high-risk steps on.")


class PolicyAppChangeSerializer(serializers.Serializer):
    app = serializers.CharField()
    source = serializers.CharField(allow_blank=True, allow_null=True)
    state = serializers.CharField()
    action = serializers.ChoiceField(choices=["install", "upgrade", "pin", "uninstall"])
    have = serializers.CharField(allow_blank=True)
    want = serializers.CharField(allow_blank=True)


class PolicyPatchChangeSerializer(serializers.Serializer):
    update = serializers.CharField()
    kind = serializers.ChoiceField(choices=["windows", "linux"])
    title = serializers.CharField(allow_blank=True)
    action = serializers.CharField()
    severity = serializers.CharField(allow_blank=True, required=False)
    want = serializers.CharField(allow_blank=True, required=False)


class PolicyDriftHostSerializer(serializers.Serializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()
    changes = serializers.ListField(
        child=PolicyAppChangeSerializer(),
        help_text="App-rule changes and patch changes (see the policy's Patching "
                  "tab) mixed in one list; the keys say which is which.")


class PolicyDriftUnknownHostSerializer(serializers.Serializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()


class PolicyDriftSerializer(serializers.Serializer):
    hosts = PolicyDriftHostSerializer(many=True)
    compliant = serializers.IntegerField()
    unknown = PolicyDriftUnknownHostSerializer(many=True)


class PolicyRunSerializer(serializers.Serializer):
    totp = serializers.CharField(write_only=True,
                                 help_text="Current TOTP code — a policy run is "
                                           "gated like a deploy.")


class PolicyRunResultSerializer(serializers.Serializer):
    dispatched = serializers.IntegerField()
    mode = serializers.ChoiceField(choices=["none", "direct", "rollout", "approval"])
    compliant = serializers.IntegerField()
    unknown = serializers.IntegerField()
    run_id = serializers.CharField(required=False, allow_null=True)
    rollout_id = serializers.CharField(required=False)
    queued = serializers.IntegerField(required=False)
    error = serializers.CharField(required=False)
    unreachable = serializers.BooleanField(required=False)
    detail = serializers.CharField(required=False)


class PolicyChangeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    policy_id = serializers.UUIDField()
    policy = serializers.CharField()
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()
    changes = serializers.ListField(
        child=serializers.DictField(),
        help_text="The run's proposed changes, stored verbatim — the same shapes "
                  "the drift endpoint returns.")
    state = serializers.CharField()
    created_at = serializers.DateTimeField()
    decided_by = serializers.CharField(allow_null=True)
    decided_at = serializers.DateTimeField(allow_null=True)


class PolicyChangeListSerializer(serializers.Serializer):
    results = PolicyChangeSerializer(many=True)


class PolicyChangeDecisionSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.CharField(),
                                allow_empty=False,
                                help_text="The PolicyChange ids to decide.")
    totp = serializers.CharField(required=False, write_only=True,
                                 help_text="Current TOTP code — approving (and "
                                           "dispatching) is gated like a deploy.")


class PolicyApproveResultSerializer(serializers.Serializer):
    approved = serializers.IntegerField()
    dispatched = serializers.IntegerField()
    refused = serializers.IntegerField(required=False,
                                       help_text="Changes kept pending because the "
                                                 "policy has not opted in to high risk.")


class PolicyRejectResultSerializer(serializers.Serializer):
    rejected = serializers.IntegerField()


class ComplianceSlaSerializer(serializers.Serializer):
    critical = serializers.IntegerField()
    important = serializers.IntegerField()
    moderate = serializers.IntegerField()
    low = serializers.IntegerField()


class ComplianceAgesSerializer(serializers.Serializer):
    """One severity's four age buckets. The response keys them ``"0-7"``,
    ``"8-30"``, ``"31-90"`` and ``"90+"``, and an object property cannot name
    what it holds, so the schema shows them as a list in that order."""

    age = serializers.IntegerField()


class ComplianceMissingByAgeSerializer(serializers.DictField):
    child = ComplianceAgesSerializer()


class PolicyComplianceSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    hosts_total = serializers.IntegerField()
    hosts_patched = serializers.IntegerField()
    patched_pct = serializers.FloatField()
    # ``{"critical": {"0-7": …}, "important": …}``: severity keys an attribute
    # cannot have, so it stays a free-form dict of the per-bucket shape.
    missing_by_age = ComplianceMissingByAgeSerializer()
    sla_days = ComplianceSlaSerializer()


class PolicyDetailErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
    needs_totp = serializers.BooleanField(required=False)
