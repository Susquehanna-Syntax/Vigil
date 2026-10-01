from django.urls import path

from . import views

urlpatterns = [
    path("", views.stack_index, name="stack-index"),
    path("<uuid:stack_id>/", views.stack_detail, name="stack-detail"),
    path("<uuid:stack_id>/revisions/", views.stack_revisions, name="stack-revisions"),
    path("<uuid:stack_id>/env/reveal/", views.stack_env_reveal, name="stack-env-reveal"),
    path("<uuid:stack_id>/deploy/", views.stack_deploy, name="stack-deploy"),
    path("<uuid:stack_id>/remove/", views.stack_remove, name="stack-remove"),
]
