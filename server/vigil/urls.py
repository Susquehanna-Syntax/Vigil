from django.contrib import admin
from django.shortcuts import render
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.alerts.models import Alert
from apps.hosts.models import Host
from apps.hosts.views import checkin, register
from apps.tasks.models import Task


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


def dashboard(request):
    hosts = Host.objects.exclude(status=Host.Status.REJECTED)
    alerts_firing = Alert.objects.filter(state=Alert.State.FIRING).select_related("host", "rule")
    alerts_ack = Alert.objects.filter(state=Alert.State.ACKNOWLEDGED).select_related("host", "rule").order_by("-fired_at")[:20]
    alerts_resolved = Alert.objects.filter(state=Alert.State.RESOLVED).select_related("host", "rule").order_by("-resolved_at")[:20]
    tasks = Task.objects.select_related("host", "requested_by").order_by("-created_at")[:50]
    pending_hosts = hosts.filter(status=Host.Status.PENDING)

    return render(request, "dashboard.html", {
        "hosts": hosts,
        "host_count": hosts.count(),
        "online_count": hosts.filter(status=Host.Status.ONLINE).count(),
        "offline_count": hosts.filter(status=Host.Status.OFFLINE).count(),
        "pending_count": pending_hosts.count(),
        "alert_count": alerts_firing.count(),
        "alerts_firing": alerts_firing,
        "alerts_ack": alerts_ack,
        "alerts_resolved": alerts_resolved,
        "tasks": tasks,
        "pending_hosts": pending_hosts,
    })


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("api/v1/health/", health_check),
    path("api/v1/register", register, name="register"),
    path("api/v1/checkin", checkin, name="checkin"),
    path("api/v1/hosts/", include("apps.hosts.urls")),
    path("api/v1/metrics/", include("apps.metrics.urls")),
    path("api/v1/alerts/", include("apps.alerts.urls")),
    path("api/v1/tasks/", include("apps.tasks.urls")),
]
