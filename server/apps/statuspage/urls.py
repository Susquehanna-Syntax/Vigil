from django.urls import path

from . import views

urlpatterns = [
    path("", views.page_index, name="statuspage-index"),
    path("<uuid:page_id>/", views.page_detail, name="statuspage-detail"),
]
