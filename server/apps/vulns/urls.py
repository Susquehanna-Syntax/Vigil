from django.urls import path

from . import views

urlpatterns = [
    path("", views.vuln_list, name="vuln-list"),
]
