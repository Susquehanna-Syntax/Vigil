from django.urls import path

from . import views

# Auto-mounted by vigil/urls.py at ext/example_extension/ when this app is
# listed in VIGIL_EXTRA_APPS.
urlpatterns = [
    path("ping/", views.ping, name="example-extension-ping"),
]
