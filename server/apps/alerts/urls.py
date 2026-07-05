from django.urls import path

from . import views

urlpatterns = [
    path("", views.alert_list, name="alert-list"),
    path("bulk/", views.alert_bulk, name="alert-bulk"),
    path("<uuid:alert_id>/acknowledge/", views.alert_acknowledge, name="alert-acknowledge"),
    path("<uuid:alert_id>/unacknowledge/", views.alert_unacknowledge, name="alert-unacknowledge"),
    path("<uuid:alert_id>/silence/", views.alert_silence, name="alert-silence"),
]