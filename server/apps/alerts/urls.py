from django.urls import path

from . import views

urlpatterns = [
    path("", views.alert_list, name="alert-list"),
    path("<uuid:alert_id>/acknowledge/", views.alert_acknowledge, name="alert-acknowledge"),
    path("<uuid:alert_id>/silence/", views.alert_silence, name="alert-silence"),
]