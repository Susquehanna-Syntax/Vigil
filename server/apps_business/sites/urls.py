from django.urls import path

from . import views

urlpatterns = [
    path("", views.site_index, name="site-list"),
    path("<uuid:site_id>/", views.site_detail, name="site-detail"),
    path("<uuid:site_id>/hosts/<uuid:host_id>/", views.site_host_assignment, name="site-host-assignment"),
]
