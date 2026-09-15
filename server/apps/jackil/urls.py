from django.urls import path

from . import views

urlpatterns = [
    path("tickets/", views.recent_tickets, name="jackil-tickets"),
]
