from django.urls import path

from . import registries, views

urlpatterns = [
    path("", views.stack_index, name="stack-index"),
    path("adopt/", views.stack_adopt, name="stack-adopt"),
    path("validate/", views.stack_validate, name="stack-validate"),
    path("registries/", registries.registry_index, name="stack-registries"),
    path("registries/<uuid:cred_id>/", registries.registry_detail, name="stack-registry-detail"),
    path("<uuid:stack_id>/", views.stack_detail, name="stack-detail"),
    path("<uuid:stack_id>/revisions/", views.stack_revisions, name="stack-revisions"),
    path("<uuid:stack_id>/env/reveal/", views.stack_env_reveal, name="stack-env-reveal"),
    path("<uuid:stack_id>/deploy/", views.stack_deploy, name="stack-deploy"),
    path("<uuid:stack_id>/remove/", views.stack_remove, name="stack-remove"),
]
