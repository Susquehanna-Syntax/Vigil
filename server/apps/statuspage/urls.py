from django.urls import path

from . import views

urlpatterns = [
    path("", views.page_index, name="statuspage-index"),
    path("hosts/", views.selectable_hosts, name="statuspage-hosts"),
    path("<uuid:page_id>/", views.page_detail, name="statuspage-detail"),
]
