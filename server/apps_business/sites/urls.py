from django.urls import path

from . import views

urlpatterns = [
    path("", views.site_index, name="site-list"),
    path("roles/", views.user_site_roles, name="user-site-roles"),
    path("roles/<int:role_id>/", views.user_site_role_detail, name="user-site-role-detail"),
    path("<uuid:site_id>/", views.site_detail, name="site-detail"),
    path("<uuid:site_id>/hosts/<uuid:host_id>/", views.site_host_assignment, name="site-host-assignment"),
]
