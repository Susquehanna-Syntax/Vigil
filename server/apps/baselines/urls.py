from django.urls import path

from . import views

urlpatterns = [
    path("", views.baseline_index, name="baseline-index"),
    path("yaml/", views.baseline_from_yaml, name="baseline-from-yaml"),
    path("<uuid:baseline_id>/", views.baseline_detail, name="baseline-detail"),
    path("<uuid:baseline_id>/yaml/", views.baseline_yaml, name="baseline-yaml"),
]
