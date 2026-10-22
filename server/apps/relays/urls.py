from django.urls import path

from . import views

urlpatterns = [
    path("", views.relay_index, name="relay-index"),
    path("<uuid:relay_id>/", views.relay_detail, name="relay-detail"),
    path("<uuid:relay_id>/rotate-key/", views.relay_rotate_key,
         name="relay-rotate-key"),
]
