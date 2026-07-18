from django.urls import path

from . import views

urlpatterns = [
    path("", views.baseline_index, name="baseline-index"),
    path("<uuid:baseline_id>/", views.baseline_detail, name="baseline-detail"),
]
