from django.urls import path

from . import views

urlpatterns = [
    path("", views.license_view, name="license"),
    path("refresh/", views.license_refresh, name="license-refresh"),
]
