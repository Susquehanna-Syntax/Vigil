from datetime import timedelta

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import include, path
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.alerts.models import Alert
from apps.hosts.models import Host
from apps.hosts.views import checkin, register
from apps.tasks.models import Task

# Hosts that haven't checked in within this window are surfaced in a
# collapsed "Inactive" section on the dashboard rather than mixed in with
# the active fleet. 90 days matches typical IT inventory aging policies.
INACTIVE_AFTER_DAYS = 90


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


@login_required(login_url="/admin/login/")
def dashboard(request):
    hosts = Host.objects.exclude(status=Host.Status.REJECTED)
    cutoff = now() - timedelta(days=INACTIVE_AFTER_DAYS)
    inactive_hosts = list(
        hosts.filter(status=Host.Status.OFFLINE, last_checkin__lt=cutoff).order_by("hostname")
    )
    inactive_ids = {h.id for h in inactive_hosts}
    active_hosts = [h for h in hosts.order_by("hostname") if h.id not in inactive_ids]

    alerts_firing = Alert.objects.filter(state=Alert.State.FIRING).select_related("host", "rule")
    alerts_ack = Alert.objects.filter(state=Alert.State.ACKNOWLEDGED).select_related("host", "rule").order_by("-fired_at")[:20]
    alerts_resolved = Alert.objects.filter(state=Alert.State.RESOLVED).select_related("host", "rule").order_by("-resolved_at")[:20]
    tasks = Task.objects.select_related("host", "requested_by").order_by("-created_at")[:50]
    pending_hosts = hosts.filter(status=Host.Status.PENDING)

    return render(request, "dashboard.html", {
        "hosts": active_hosts,
        "active_hosts": active_hosts,
        "inactive_hosts": inactive_hosts,
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
    path("api/v1/vulns/", include("apps.vulns.urls")),
    path("api/v1/accounts/", include("apps.accounts.urls")),
]
