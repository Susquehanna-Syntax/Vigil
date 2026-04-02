from django.contrib import admin

from .models import MetricPoint


@admin.register(MetricPoint)
class MetricPointAdmin(admin.ModelAdmin):
    list_display = ("host", "category", "metric", "value", "time")
    list_filter = ("category",)
    search_fields = ("host__hostname", "metric")
