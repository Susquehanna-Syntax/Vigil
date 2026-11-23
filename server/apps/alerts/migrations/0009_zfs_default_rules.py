from django.db import migrations

ZFS_RULES = [
    {
        "name": "ZFS pool not healthy",
        "category": "zfs",
        "metric": "pools_unhealthy",
        "operator": "gt",
        "threshold": 0.0,
        "severity": "critical",
        "duration_seconds": 0,
    },
    {
        "name": "ZFS pool nearly full (85%)",
        "category": "zfs",
        "metric": "max_capacity_percent",
        "operator": "gt",
        "threshold": 85.0,
        "severity": "warning",
        "duration_seconds": 300,
    },
    {
        "name": "ZFS pool errors",
        "category": "zfs",
        "metric": "total_errors",
        "operator": "gt",
        "threshold": 0.0,
        "severity": "warning",
        "duration_seconds": 0,
    },
    {
        "name": "ZFS scrub overdue (35 days)",
        "category": "zfs",
        "metric": "max_scrub_age_days",
        "operator": "gt",
        "threshold": 35.0,
        "severity": "warning",
        "duration_seconds": 0,
    },
]


def add_rules(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    for rule_data in ZFS_RULES:
        AlertRule.objects.get_or_create(
            name=rule_data["name"],
            defaults={**rule_data, "enabled": True, "is_default": True},
        )


def remove_rules(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    AlertRule.objects.filter(name__in=[r["name"] for r in ZFS_RULES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0008_alert_source_key"),
    ]

    operations = [
        migrations.RunPython(add_rules, remove_rules),
    ]
