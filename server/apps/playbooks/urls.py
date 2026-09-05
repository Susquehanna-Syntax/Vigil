from django.urls import path

from . import views

urlpatterns = [
    path("", views.playbook_index, name="playbook-index"),
    path("yaml/", views.playbook_from_yaml, name="playbook-from-yaml"),
    path("<uuid:playbook_id>/", views.playbook_detail, name="playbook-detail"),
    path("<uuid:playbook_id>/yaml/", views.playbook_yaml, name="playbook-yaml"),
]

#: The pre-2026.11.0 path, mounted at /api/v1/baselines/ so anything an
#: operator already scripted keeps working. Deliberately unnamed: a duplicate
#: name would win the reverse() lookup and Vigil would start emitting the
#: legacy path everywhere.
legacy_urlpatterns = [
    path("", views.playbook_index),
    path("yaml/", views.playbook_from_yaml),
    path("<uuid:playbook_id>/", views.playbook_detail),
    path("<uuid:playbook_id>/yaml/", views.playbook_yaml),
]
