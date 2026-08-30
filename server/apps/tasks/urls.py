from django.urls import path

from . import views

urlpatterns = [
    path("history/", views.task_history, name="task-history"),
    path("result/", views.task_result, name="task-result"),
    path("actions/", views.action_registry, name="task-actions"),
    # The bare path keeps the pre-2026.9.1 URL working; it means tasks.
    path("community/", views.community_templates, name="community-templates"),
    path("community/<str:kind>/", views.community_templates,
         name="community-templates-kind"),
    path("community/<str:kind>/<str:filename>/plan/", views.community_fork_plan,
         name="community-fork-plan"),
    path("community/<str:kind>/<str:filename>/fork/", views.community_fork,
         name="community-fork"),
    path("definitions/", views.definition_list, name="definition-list"),
    path("definitions/validate/", views.definition_validate, name="definition-validate"),
    path("definitions/<uuid:definition_id>/", views.definition_detail, name="definition-detail"),
    path("definitions/<uuid:definition_id>/fork/", views.definition_fork, name="definition-fork"),
    path("definitions/<uuid:definition_id>/deploy/", views.definition_deploy, name="definition-deploy"),
    path("runs/", views.run_history, name="run-history"),
    path("runs/<uuid:run_id>/", views.run_detail, name="run-detail"),
    path("<uuid:task_id>/", views.task_detail, name="task-detail"),
]
