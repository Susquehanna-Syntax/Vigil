from django.urls import path

from . import integration_views, integration_widgets, views

urlpatterns = [
    path("tickets/", views.recent_tickets, name="jackil-tickets"),
    path("integration-keys/", views.integration_keys, name="jackil-integration-keys"),
    path("integration-keys/<int:pk>/revoke/", views.revoke_integration_key,
         name="jackil-integration-key-revoke"),
    # A session endpoint for any signed-in user, unlike the key-only routes
    # below it; listed ahead of "integration/" so the two surfaces stay legible.
    path("related/", views.related, name="jackil-related"),
    # The read-only surface a key presents itself with. Deliberately a
    # separate namespace from the admin's own session endpoints above.
    path("integration/ping/", integration_views.ping, name="jackil-integration-ping"),
    path("integration/search/", integration_views.search, name="jackil-integration-search"),
    path("integration/resolve/", integration_views.resolve, name="jackil-integration-resolve"),
    # Ahead of the `<str:kind>` detail route: without it "widgets" would be
    # read as a type and answered with "Unknown type".
    path("integration/widgets/", integration_widgets.widgets,
         name="jackil-integration-widgets"),
    path("integration/widgets/<str:kind>/", integration_widgets.widget,
         name="jackil-integration-widget"),
    path("integration/<str:kind>/<str:id>/", integration_views.detail,
         name="jackil-integration-detail"),
]
