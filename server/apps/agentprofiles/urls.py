from django.urls import path

from . import views

urlpatterns = [
    path("", views.profile_index, name="agentprofile-index"),
    path("actions/", views.actions_list, name="agentprofile-actions"),
    path("enroll-config/", views.enroll_config, name="agentprofile-enroll-config"),
    path("<uuid:profile_id>/", views.profile_detail, name="agentprofile-detail"),
]
