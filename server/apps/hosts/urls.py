from django.urls import path

from . import views

urlpatterns = [
    path("", views.host_list, name="host-list"),
    path("tags/", views.tag_index, name="tag-index"),
    path("inventory/", views.inventory_list, name="inventory-list"),
    path("ad/", views.ad_config, name="ad-config"),
    path("ad/sync/", views.ad_sync_now, name="ad-sync"),
    path("<uuid:host_id>/", views.host_detail, name="host-detail"),
    path("<uuid:host_id>/approve/", views.host_approve, name="host-approve"),
    path("<uuid:host_id>/reject/", views.host_reject, name="host-reject"),
    path("<uuid:host_id>/poll/", views.host_poll, name="host-poll"),
    path("<uuid:host_id>/rdp/", views.host_rdp, name="host-rdp"),
    path("<uuid:host_id>/tags/", views.host_tags, name="host-tags"),
    path("<uuid:host_id>/inventory/", views.inventory_detail, name="inventory-detail"),
    path("check-pending/", views.check_pending, name="host-check-pending"),
]