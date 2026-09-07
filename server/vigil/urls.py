from django.conf import settings as django_settings
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.views import login_view, logout_view, setup_view
from apps.alerts.models import Alert
from apps.hosts.models import Host
from apps.hosts import views as hosts_views
from apps.hosts.views import checkin, register
from apps.playbooks.urls import legacy_urlpatterns as _legacy_playbook_urls
from apps.reprovision.installer_views import enroll as reprovision_enroll
from apps.statuspage.views import public_status as status_public_view
from apps.statuspage.views import public_status_data as status_public_data_view


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def about(request):
    """Server build + scanner status, surfaced by the About page.

    Session-gated: exact server/Python versions, the database vendor,
    and which scanners are configured are exactly what an attacker
    fingerprints a deployment with. Operators pasting build info into a
    support thread can log in first — the About page already lives
    behind the dashboard.
    """
    import sys

    from apps.vulns.scanners import SCANNER_REGISTRY

    db_vendor = (
        django_settings.DATABASES.get("default", {}).get("ENGINE", "").rsplit(".", 1)[-1]
    )

    scanners = []
    for name, cls in SCANNER_REGISTRY.items():
        try:
            configured = cls().configured()
        except Exception:
            configured = False
        scanners.append({"name": name, "configured": configured})

    from vigil.editions import active_edition, enabled_features

    return Response({
        "vigil_version": getattr(django_settings, "VIGIL_VERSION", "unknown"),
        "expected_agent_version": getattr(django_settings, "VIGIL_AGENT_VERSION", ""),
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "database": db_vendor,
        "timezone": getattr(django_settings, "VIGIL_TIMEZONE", "UTC"),
        "scanners": scanners,
        "edition": active_edition(),
        "features": sorted(enabled_features()),
    })


@login_required(login_url="/login/")
def dashboard(request):
    # The dashboard page itself is the widget grid; these feed the pages that
    # still render host lists server-side (the Monitor host dropdown and the
    # Settings "All Agents" table) plus the header sub-line counts.
    hosts = Host.objects.exclude(status=Host.Status.REJECTED).select_related("inventory").order_by("hostname")
    pending_hosts = hosts.filter(status=Host.Status.PENDING)

    return render(request, "dashboard.html", {
        "hosts": list(hosts),
        "host_count": hosts.count(),
        "online_count": hosts.filter(status=Host.Status.ONLINE).count(),
        "pending_count": pending_hosts.count(),
        "alert_count": Alert.objects.filter(state=Alert.State.FIRING).count(),
        "pending_hosts": pending_hosts,
        "vigil_timezone": django_settings.VIGIL_TIMEZONE,
        "vigil_time_format": django_settings.VIGIL_TIME_FORMAT,
        "vigil_username": request.user.username,
        "vigil_public_url": django_settings.VIGIL_PUBLIC_URL,
    })


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("setup/", setup_view, name="setup"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("admin/", admin.site.urls),
    path("api/v1/health/", health_check),
    path("api/v1/about/", about, name="about-api"),
    path("api/v1/register", register, name="register"),
    path("api/v1/checkin", checkin, name="checkin"),
    path("api/v1/hosts/", include("apps.hosts.urls")),
    path("api/v1/metrics/", include("apps.metrics.urls")),
    path("api/v1/alerts/", include("apps.alerts.urls")),
    path("api/v1/tasks/", include("apps.tasks.urls")),
    path("api/v1/", include("apps.tasks.rollout_urls")),
    path("api/v1/tags/", hosts_views.tag_collection, name="tag-collection"),
    path("api/v1/tags/<int:tag_id>/", hosts_views.tag_detail, name="tag-detail"),
    path("api/v1/vulns/", include("apps.vulns.urls")),
    path("api/v1/accounts/", include("apps.accounts.urls")),
    path("api/v1/sites/", include("apps_business.sites.urls")),
    path("api/v1/branding/", include("apps_business.branding.urls")),
    path("api/v1/audits/", include("apps_business.audits.urls")),
    path("api/v1/license/", include("apps.licensing.urls")),
    path("api/v1/playbooks/", include("apps.playbooks.urls")),
    path("api/v1/dashboards/", include("apps.dashboards.urls")),
    # Playbooks were called baselines until 2026.11.0. Anything an operator
    # already scripted against the old path keeps working; nothing in Vigil
    # emits it any more.
    path("api/v1/baselines/", include(_legacy_playbook_urls)),
    path("api/v1/ai/", include("apps.aisuggest.urls")),
    path("api/v1/status-pages/", include("apps.statuspage.urls")),
    path("api/v1/automations/", include("apps.automations.urls")),
    path("api/v1/reprovision/", include("apps.reprovision.urls")),
    # Unauthenticated by necessity — the installer and the freshly-installed
    # agent hold no credentials. Security is the unguessable one-time token;
    # see docs/reprovisioning.md §4.2 and §4.3.
    path("api/v1/reprovision/enroll", reprovision_enroll, name="reprovision-enroll"),
    path("reprovision/", include("apps.reprovision.installer_urls")),
    path("", include("apps.civilsso.urls")),
    path("agent/", include("apps.agent_dist.urls")),
    path("status/<str:token>/", status_public_view, name="status-public"),
    path("status/<str:token>/data/", status_public_data_view, name="status-public-data"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


# ---------------------------------------------------------------------------
# Edition extension URLs (Pro / Enterprise)
# ---------------------------------------------------------------------------
# Each app named in VIGIL_EXTRA_APPS may expose a ``urls.py``; if present it is
# mounted under ``ext/<app-label>/`` (the app's final dotted segment). Apps
# without a urls module are skipped. Core never imports edition code directly —
# routes are discovered, not hard-wired. See docs/pro-extension-points.md.
def _edition_url_patterns():
    from importlib.util import find_spec

    patterns = []
    for app_path in getattr(settings, "VIGIL_EXTRA_APPS", []):
        urls_module = f"{app_path}.urls"
        try:
            if find_spec(urls_module) is None:
                continue
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
        label = app_path.rsplit(".", 1)[-1]
        patterns.append(path(f"ext/{label}/", include(urls_module)))
    return patterns


urlpatterns += _edition_url_patterns()
