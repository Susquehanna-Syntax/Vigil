from django.urls import path

from . import views

urlpatterns = [
    path("settings/", views.ai_settings, name="ai-settings"),
    path("suggest/alert/<uuid:alert_id>/", views.suggest_for_alert,
         name="ai-suggest-alert"),
]
