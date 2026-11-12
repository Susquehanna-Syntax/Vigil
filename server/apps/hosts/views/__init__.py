"""apps.hosts.views, split by concern (sprint M5). Every name the old module had is
importable from here as before."""
from ._common import *  # noqa: F401,F403
from .agent import register, checkin, transport_ack, host_poll, check_pending  # noqa: F401
from .hosts import host_list, host_detail, host_approve, host_reject, host_tags, host_update_agent, host_rdp, unmanaged_devices, unmanaged_device_detail, ad_config, ad_sync_now  # noqa: F401
from .inventory import tag_index, inventory_list, inventory_detail, host_stacks, host_containers, docker_overview  # noqa: F401
from .firewall import host_firewall, host_firewall_refresh, host_firewall_apply  # noqa: F401
from .tags import tag_collection, tag_detail  # noqa: F401
