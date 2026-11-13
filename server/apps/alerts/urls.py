from django.urls import path

from . import channel_views, views

urlpatterns = [
    path("", views.alert_list, name="alert-list"),
    path("rules/", views.rule_list, name="alert-rule-list"),
    path("bulk/", views.alert_bulk, name="alert-bulk"),
    path("<uuid:alert_id>/acknowledge/", views.alert_acknowledge, name="alert-acknowledge"),
    path("<uuid:alert_id>/unacknowledge/", views.alert_unacknowledge, name="alert-unacknowledge"),
    path("<uuid:alert_id>/silence/", views.alert_silence, name="alert-silence"),
    # Notification channels (sprint M7).
    path("channels/", channel_views.channel_index, name="alert-channel-index"),
    path("channels/types/", channel_views.channel_types, name="alert-channel-types"),
    path("channels/<uuid:channel_id>/", channel_views.channel_detail, name="alert-channel-detail"),
    path("channels/<uuid:channel_id>/test/", channel_views.channel_test, name="alert-channel-test"),
]