from django.urls import path

from . import views

urlpatterns = [
    path("", views.automation_index, name="automation-index"),
    path("<uuid:automation_id>/", views.automation_detail, name="automation-detail"),
    path("<uuid:automation_id>/run/", views.automation_run_now, name="automation-run"),
]
