from django.urls import path

from . import update_views, views

urlpatterns = [
    path("apps/", views.app_list, name="software-app-list"),
    path("apps/<str:name_key>/", views.app_detail, name="software-app-detail"),
    path("hosts/", views.host_summaries, name="software-host-summaries"),
    path("updates/", update_views.update_list, name="software-update-list"),
    path("updates/hosts/", update_views.update_hosts, name="software-update-hosts"),
    path("updates/decide/", update_views.update_decide, name="software-update-decide"),
    path("hosts/<uuid:host_id>/", views.host_software, name="software-host-detail"),
]
