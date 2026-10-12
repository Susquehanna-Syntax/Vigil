from django.urls import path

from . import docker_views, logtail, topology, views

urlpatterns = [
    path("", views.host_list, name="host-list"),
    path("tags/", views.tag_index, name="tag-index"),
    path("inventory/", views.inventory_list, name="inventory-list"),
    path("docker/overview/", views.docker_overview, name="docker-overview"),
    path("devices/", views.unmanaged_devices, name="unmanaged-devices"),
    path("devices/<uuid:device_id>/", views.unmanaged_device_detail, name="unmanaged-device-detail"),
    path("transport-ack/", views.transport_ack, name="transport-ack"),
    path("ad/", views.ad_config, name="ad-config"),
    path("ad/sync/", views.ad_sync_now, name="ad-sync"),
    # Before any <uuid:host_id>/ path: "topology/" must not be swallowed by
    # the host-detail route, whose converter would reject it with a 404.
    path("topology/", topology.topology, name="host-topology"),
    path("<uuid:host_id>/firewall/", views.host_firewall, name="host-firewall"),
    path("<uuid:host_id>/firewall/refresh/", views.host_firewall_refresh,
         name="host-firewall-refresh"),
    path("<uuid:host_id>/firewall/apply/", views.host_firewall_apply,
         name="host-firewall-apply"),
    path("<uuid:host_id>/", views.host_detail, name="host-detail"),
    path("<uuid:host_id>/approve/", views.host_approve, name="host-approve"),
    path("<uuid:host_id>/reject/", views.host_reject, name="host-reject"),
    path("<uuid:host_id>/poll/", views.host_poll, name="host-poll"),
    path("<uuid:host_id>/update-agent/", views.host_update_agent, name="host-update-agent"),
    path("<uuid:host_id>/rdp/", views.host_rdp, name="host-rdp"),
    path("<uuid:host_id>/tags/", views.host_tags, name="host-tags"),
    path("<uuid:host_id>/docker/", docker_views.host_docker, name="host-docker"),
    path("docker-summary/", docker_views.host_docker_summary, name="host-docker-summary"),
    path("<uuid:host_id>/inventory/", views.inventory_detail, name="inventory-detail"),
    path("<uuid:host_id>/containers/", views.host_containers, name="host-containers"),
    path("<uuid:host_id>/stacks/", views.host_stacks, name="host-stacks"),
    path("<uuid:host_id>/containers/<str:name>/logs/", logtail.open_logs, name="host-container-logs"),
    path("log-tails/<uuid:session_id>/", logtail.poll_logs, name="log-tail"),
    path("check-pending/", views.check_pending, name="host-check-pending"),
]
