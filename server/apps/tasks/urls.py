from django.urls import path

from . import views

urlpatterns = [
    path("", views.task_list, name="task-list"),
    path("result/", views.task_result, name="task-result"),
]