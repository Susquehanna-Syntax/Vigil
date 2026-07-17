from django.urls import path

from . import views

urlpatterns = [
    path("", views.license_view, name="license"),
]
