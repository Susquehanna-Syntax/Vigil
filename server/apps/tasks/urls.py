from django.urls import path

from . import views

urlpatterns = [
    path("", views.task_list, name="task-list"),
    path("result/", views.task_result, name="task-result"),
    path("actions/", views.action_registry, name="task-actions"),
    path("definitions/", views.definition_list, name="definition-list"),
    path("definitions/validate/", views.definition_validate, name="definition-validate"),
    path("definitions/<uuid:definition_id>/", views.definition_detail, name="definition-detail"),
    path("definitions/<uuid:definition_id>/fork/", views.definition_fork, name="definition-fork"),
    path("definitions/<uuid:definition_id>/publish/", views.definition_publish, name="definition-publish"),
    path("definitions/<uuid:definition_id>/unpublish/", views.definition_unpublish, name="definition-unpublish"),
    path("definitions/<uuid:definition_id>/deploy/", views.definition_deploy, name="definition-deploy"),
    path("runs/<uuid:run_id>/", views.run_detail, name="run-detail"),
]
