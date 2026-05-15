from django.urls import path

from . import views

urlpatterns = [
    path("", views.vuln_list, name="vuln-list"),
    path("scans/", views.scan_list, name="scan-list"),
    path("scans/<uuid:host_id>/", views.scan_create, name="scan-create"),
]
