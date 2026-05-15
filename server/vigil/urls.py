from datetime import timedelta

from django.conf import settings as django_settings
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import include, path
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.accounts.views import login_view, logout_view, setup_view
from apps.alerts.models import Alert
from apps.hosts.models import Host
from apps.hosts.views import checkin, register

# Hosts that haven't checked in within this window are surfaced in a
# collapsed "Inactive" section on the dashboard rather than mixed in with
# the active fleet. 90 days matches typical IT inventory aging policies.
INACTIVE_AFTER_DAYS = 90


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


@login_required(login_url="/login/")
def dashboard(request):
    hosts = Host.objects.exclude(status=Host.Status.REJECTED).select_related("inventory")
    cutoff = now() - timedelta(days=INACTIVE_AFTER_DAYS)
    inactive_hosts = list(
        hosts.filter(status=Host.Status.OFFLINE, last_checkin__lt=cutoff).order_by("hostname")
    )
    inactive_ids = {h.id for h in inactive_hosts}
    active_hosts = [h for h in hosts.order_by("hostname") if h.id not in inactive_ids]

    alerts_firing = Alert.objects.filter(state=Alert.State.FIRING).select_related("host", "rule")
    alerts_ack = Alert.objects.filter(state=Alert.State.ACKNOWLEDGED).select_related("host", "rule").order_by("-fired_at")[:20]
    alerts_resolved = Alert.objects.filter(state=Alert.State.RESOLVED).select_related("host", "rule").order_by("-resolved_at")[:20]
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
        "pending_hosts": pending_hosts,
        "vigil_timezone": django_settings.VIGIL_TIMEZONE,
        "vigil_time_format": django_settings.VIGIL_TIME_FORMAT,
        "vigil_username": request.user.username,
    })


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("setup/", setup_view, name="setup"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
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
    path("agent/", include("apps.agent_dist.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
