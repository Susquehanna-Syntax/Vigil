from django.db import migrations

EXTRA_RULES = [
    {
        "name": "CPU Critical (95%)",
        "category": "cpu",
        "metric": "usage_percent",
        "operator": "gt",
        "threshold": 95.0,
        "severity": "critical",
        "duration_seconds": 60,
    },
    {
        "name": "High Load Average (1m)",
        "category": "cpu",
        "metric": "load_1m",
        "operator": "gt",
        "threshold": 10.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
    {
        "name": "High Load Average (5m)",
        "category": "cpu",
        "metric": "load_5m",
        "operator": "gt",
        "threshold": 8.0,
        "severity": "warning",
        "duration_seconds": 300,
    },
    {
        "name": "Sustained High Load (15m)",
        "category": "cpu",
        "metric": "load_15m",
        "operator": "gt",
        "threshold": 6.0,
        "severity": "critical",
        "duration_seconds": 600,
    },
    {
        "name": "Memory Critical (95%)",
        "category": "memory",
        "metric": "usage_percent",
        "operator": "gt",
        "threshold": 95.0,
        "severity": "critical",
        "duration_seconds": 60,
    },
    {
        "name": "Swap Nearly Exhausted",
        "category": "memory",
        "metric": "swap_usage_percent",
        "operator": "gt",
        "threshold": 80.0,
        "severity": "critical",
        "duration_seconds": 120,
    },
    {
        "name": "Disk Critical (95%)",
        "category": "disk",
        "metric": "usage_percent",
        "operator": "gt",
        "threshold": 95.0,
        "severity": "critical",
        "duration_seconds": 0,
    },
    {
        "name": "High Network Error Rate (In)",
        "category": "network",
        "metric": "errors_in",
        "operator": "gt",
        "threshold": 100.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
    {
        "name": "High Network Error Rate (Out)",
        "category": "network",
        "metric": "errors_out",
        "operator": "gt",
        "threshold": 100.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
    {
        "name": "High Network Drop Rate (In)",
        "category": "network",
        "metric": "drops_in",
        "operator": "gt",
        "threshold": 200.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
    {
        "name": "High Network Drop Rate (Out)",
        "category": "network",
        "metric": "drops_out",
        "operator": "gt",
        "threshold": 200.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
    {
        "name": "Process CPU Spike",
        "category": "process",
        "metric": "cpu_percent",
        "operator": "gt",
        "threshold": 95.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
    {
        "name": "Process Memory Spike",
        "category": "process",
        "metric": "memory_percent",
        "operator": "gt",
        "threshold": 50.0,
        "severity": "warning",
        "duration_seconds": 120,
    },
]


def add_rules(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    for rule_data in EXTRA_RULES:
        AlertRule.objects.get_or_create(
            name=rule_data["name"],
            defaults={**rule_data, "enabled": True, "is_default": True},
        )


def remove_rules(apps, schema_editor):
    AlertRule = apps.get_model("alerts", "AlertRule")
    AlertRule.objects.filter(name__in=[r["name"] for r in EXTRA_RULES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0003_default_alert_rules"),
    ]

    operations = [
        migrations.RunPython(add_rules, remove_rules),
    ]
