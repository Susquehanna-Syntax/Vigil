from django.urls import path

from . import views

urlpatterns = [
    path("", views.vuln_list, name="vuln-list"),
    path("score/", views.fleet_score, name="vuln-fleet-score"),
    path("findings/", views.finding_list, name="vuln-finding-list"),
    path("data/", views.vuln_data, name="vuln-data"),
    path("anvil/", views.anvil_import, name="vuln-anvil-import"),
    path("fix-groups/", views.fix_group_list, name="vuln-fix-groups"),
    path("fix-groups/deploy/", views.fix_group_deploy, name="vuln-fix-group-deploy"),
    path("scans/", views.scan_list, name="scan-list"),
    path("scans/<uuid:host_id>/", views.scan_create, name="scan-create"),
    path("history/<uuid:host_id>/", views.score_history, name="vuln-score-history"),
]
