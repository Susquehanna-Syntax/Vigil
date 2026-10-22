from django.urls import include, path

from . import views

urlpatterns = [
    path("", views.session_collection, name="session-collection"),
    # Before <uuid:session_id>/: a uuid converter does not match "options", but
    # ordering it first keeps the name resolvable either way.
    path("options/", views.session_options, name="session-options"),
    path("<uuid:session_id>/", include("apps.remote.session_urls")),
]
