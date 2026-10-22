from django.urls import include, path

from . import views

urlpatterns = [
    path("", views.session_collection, name="session-collection"),
    path("<uuid:session_id>/", include("apps.remote.session_urls")),
]
