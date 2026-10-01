from django.urls import path

from . import views

urlpatterns = [
    path("", views.policy_index, name="policy-index"),
    path("compliance/", views.compliance_summary, name="policy-compliance"),
    path("changes/", views.change_list, name="policy-change-list"),
    path("changes/approve/", views.change_approve, name="policy-change-approve"),
    path("changes/reject/", views.change_reject, name="policy-change-reject"),
    path("<uuid:policy_id>/", views.policy_detail, name="policy-detail"),
    path("<uuid:policy_id>/drift/", views.policy_drift_view, name="policy-drift"),
    path("<uuid:policy_id>/run/", views.policy_run_now, name="policy-run"),
]
