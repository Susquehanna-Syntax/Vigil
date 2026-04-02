from django.contrib import admin
from django.urls import include, path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health_check(request):
    return Response({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", health_check),
    path("api/v1/hosts/", include("apps.hosts.urls")),
    path("api/v1/metrics/", include("apps.metrics.urls")),
    path("api/v1/alerts/", include("apps.alerts.urls")),
    path("api/v1/tasks/", include("apps.tasks.urls")),
]
