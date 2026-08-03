"""Authenticated reprovisioning API. The unauthenticated installer routes
live in installer_urls.py, deliberately kept apart."""
from django.urls import path

from . import views

urlpatterns = [
    path("images/", views.image_list, name="reprovision-images"),
    path("images/<uuid:image_id>/", views.image_detail,
         name="reprovision-image-detail"),
    path("profiles/", views.profile_list, name="reprovision-profiles"),
    path("profiles/<uuid:profile_id>/", views.profile_detail,
         name="reprovision-profile-detail"),
    path("profiles/<uuid:profile_id>/preview/", views.profile_preview,
         name="reprovision-profile-preview"),
    path("preflight/", views.preflight_check, name="reprovision-preflight"),
    path("jobs/", views.job_list, name="reprovision-jobs"),
    path("jobs/<uuid:job_id>/", views.job_detail,
         name="reprovision-job-detail"),
    path("jobs/<uuid:job_id>/abort/", views.job_abort,
         name="reprovision-job-abort"),
]
