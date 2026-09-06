from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard_list, name="dashboard-list"),
    path("catalog/", views.catalog, name="dashboard-catalog"),
    path("<uuid:dashboard_id>/", views.dashboard_detail, name="dashboard-detail"),
    path("<uuid:dashboard_id>/layout/", views.layout_update, name="dashboard-layout"),
    path("<uuid:dashboard_id>/share/", views.dashboard_share, name="dashboard-share"),
]
