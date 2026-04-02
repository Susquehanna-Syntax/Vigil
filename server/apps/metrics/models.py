from django.db import models

from apps.hosts.models import Host


class MetricPoint(models.Model):
    """Individual metric data point from an agent check-in.

    Designed for TimescaleDB hypertable conversion on the `time` column.
    """

    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="metric_points")
    time = models.DateTimeField(db_index=True)
    category = models.CharField(max_length=50, db_index=True)  # cpu, memory, disk, network, etc.
    metric = models.CharField(max_length=100, db_index=True)  # usage_percent, bytes_in, etc.
    value = models.FloatField()
    labels = models.JSONField(default=dict, blank=True)  # e.g. {"interface": "eth0", "mount": "/"}

    class Meta:
        indexes = [
            models.Index(fields=["host", "category", "metric", "time"]),
        ]

    def __str__(self):
        return f"{self.host.hostname}/{self.category}.{self.metric} = {self.value} @ {self.time}"
