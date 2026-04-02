from django.contrib import admin
from django.shortcuts import render
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.alerts.models import Alert
from apps.hosts.models import Host


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


def dashboard(request):
    hosts = Host.objects.exclude(status=Host.Status.REJECTED)
    alerts = Alert.objects.filter(state=Alert.State.FIRING).select_related("host")[:10]
    return render(request, "dashboard.html", {
        "hosts": hosts,
        "host_count": hosts.count(),
        "online_count": hosts.filter(status=Host.Status.ONLINE).count(),
        "pending_count": hosts.filter(status=Host.Status.PENDING).count(),
        "alert_count": alerts.count(),
        "alerts": alerts,
    })


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("api/v1/health/", health_check),
    path("api/v1/hosts/", include("apps.hosts.urls")),
    path("api/v1/metrics/", include("apps.metrics.urls")),
    path("api/v1/alerts/", include("apps.alerts.urls")),
    path("api/v1/tasks/", include("apps.tasks.urls")),
]
