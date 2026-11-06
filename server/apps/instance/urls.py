from django.urls import path

from . import views

urlpatterns = [
    path("", views.instance_settings, name="instance-settings"),
    path("test/", views.test_integration, name="instance-settings-test"),
    path("backups/", views.backup_runs, name="instance-backup-runs"),
]
