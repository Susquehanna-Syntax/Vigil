from django.urls import path

from . import views

urlpatterns = [
    path("", views.host_list, name="host-list"),
    path("<uuid:host_id>/", views.host_detail, name="host-detail"),
    path("<uuid:host_id>/approve/", views.host_approve, name="host-approve"),
    path("<uuid:host_id>/poll/", views.host_poll, name="host-poll"),
]