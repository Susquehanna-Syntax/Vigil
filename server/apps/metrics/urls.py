from django.urls import path

from . import views

urlpatterns = [
    path("catalog/", views.metric_catalog, name="metric-catalog"),
    path(
        "<uuid:host_id>/<str:category>/<str:metric_name>/",
        views.metric_history,
        name="metric-history",
    ),
]