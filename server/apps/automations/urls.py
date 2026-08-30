from django.urls import path

from . import views

urlpatterns = [
    path("", views.automation_index, name="automation-index"),
    path("yaml/", views.automation_from_yaml, name="automation-from-yaml"),
    path("<uuid:automation_id>/", views.automation_detail, name="automation-detail"),
    path("<uuid:automation_id>/yaml/", views.automation_yaml, name="automation-yaml"),
    path("<uuid:automation_id>/run/", views.automation_run_now, name="automation-run"),
]
