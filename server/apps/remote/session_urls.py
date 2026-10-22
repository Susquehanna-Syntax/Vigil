from django.urls import path

from . import views

urlpatterns = [
    path("close/", views.session_close, name="session-close"),
    path("recording/", views.session_recording, name="session-recording"),
]
