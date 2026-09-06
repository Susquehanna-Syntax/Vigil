"""Rollout API routes, mounted at /api/v1/rollouts/ (see vigil/urls.py).

Lives outside apps/tasks/urls.py because the phase doc pins the top-level
/api/v1/rollouts/ prefix, not /api/v1/tasks/rollouts/.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("rollouts/", views.rollout_collection, name="rollout-collection"),
    path("rollouts/<uuid:rollout_id>/", views.rollout_detail, name="rollout-detail"),
    path("rollouts/<uuid:rollout_id>/halt/", views.rollout_halt, name="rollout-halt"),
    path("rollouts/<uuid:rollout_id>/resume/", views.rollout_resume, name="rollout-resume"),
    path("rollouts/<uuid:rollout_id>/skip-validation/",
         views.rollout_skip_validation, name="rollout-skip-validation"),
    path("rollouts/<uuid:rollout_id>/waves/<int:wave_id>/hosts/",
         views.rollout_wave_hosts, name="rollout-wave-hosts"),
    path("wave-groups/", views.wave_group_collection, name="wave-group-collection"),
    path("waves/", views.wave_collection, name="wave-collection"),
    path("waves/<int:wave_id>/", views.wave_detail, name="wave-detail"),
]
