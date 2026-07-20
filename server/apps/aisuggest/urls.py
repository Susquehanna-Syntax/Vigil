from django.urls import path

from . import views

urlpatterns = [
    path("settings/", views.ai_settings, name="ai-settings"),
    path("suggest/alert/<uuid:alert_id>/", views.suggest_for_alert,
         name="ai-suggest-alert"),
    path("suggest/docker/<uuid:host_id>/<str:container_id>/",
         views.suggest_for_container, name="ai-suggest-container"),
]
