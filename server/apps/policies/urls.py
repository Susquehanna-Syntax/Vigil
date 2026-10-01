from django.urls import path

from . import views

urlpatterns = [
    path("", views.policy_index, name="policy-index"),
    path("<uuid:policy_id>/", views.policy_detail, name="policy-detail"),
]
