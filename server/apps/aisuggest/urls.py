from django.urls import path

from . import views

urlpatterns = [
    path("providers/", views.providers, name="ai-providers"),
    path(
        "providers/<int:provider_id>/", views.provider_detail, name="ai-provider-detail"
    ),
    path(
        "suggest/alert/<uuid:alert_id>/",
        views.suggest_for_alert,
        name="ai-suggest-alert",
    ),
    path(
        "suggest/vuln/<uuid:finding_id>/",
        views.suggest_for_vuln,
        name="ai-suggest-vuln",
    ),
]
