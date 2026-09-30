from django.urls import path

from . import views

urlpatterns = [
    path("apps/", views.app_list, name="software-app-list"),
    path("apps/<str:name_key>/", views.app_detail, name="software-app-detail"),
    path("hosts/", views.host_summaries, name="software-host-summaries"),
    path("hosts/<uuid:host_id>/", views.host_software, name="software-host-detail"),
]
