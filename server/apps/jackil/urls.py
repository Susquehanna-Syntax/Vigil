from django.urls import path

from . import integration_views, views

urlpatterns = [
    path("tickets/", views.recent_tickets, name="jackil-tickets"),
    path("integration-keys/", views.integration_keys, name="jackil-integration-keys"),
    path("integration-keys/<int:pk>/revoke/", views.revoke_integration_key,
         name="jackil-integration-key-revoke"),
    # The read-only surface a key presents itself with. Deliberately a
    # separate namespace from the admin's own session endpoints above.
    path("integration/ping/", integration_views.ping, name="jackil-integration-ping"),
    path("integration/search/", integration_views.search, name="jackil-integration-search"),
    path("integration/resolve/", integration_views.resolve, name="jackil-integration-resolve"),
    path("integration/<str:kind>/<str:id>/", integration_views.detail,
         name="jackil-integration-detail"),
]
