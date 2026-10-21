"""The gate on agent paths.

Two jobs, both about one promise: *the dashboard can stay local while only the
relay is public.*

1. A request that came through the link carries ``X-Vigil-Link``. Only then are
   ``X-Vigil-Agent-IP`` (the agent's real address, which the checkin already
   trusts over the socket) and ``X-Vigil-Relay`` (which relay it came through)
   believed. Without a valid link those headers are ignored — an agent on the
   LAN must not be able to claim another machine's address.
2. When the built-in relay is disabled, a request that did *not* come through
   the link is refused. That is what "direct connections are off" means.
"""

import ipaddress
import uuid

from django.http import JsonResponse

from .models import direct_allowed
from .trust import AGENT_IP_HEADER, RELAY_HEADER, from_link, is_agent_path


class RelayGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not is_agent_path(request.path):
            return self.get_response(request)

        if from_link(request):
            ip = request.META.get(AGENT_IP_HEADER, "")
            try:
                request.META["REMOTE_ADDR"] = str(ipaddress.ip_address(ip))
            except ValueError:
                pass
            relay_id = self._relay_id(request.META.get(RELAY_HEADER, ""))
            if relay_id is None:
                return JsonResponse({"error": "Unknown relay"}, status=403)
            request.vigil_relay_id = relay_id
            return self.get_response(request)

        if not direct_allowed():
            return JsonResponse(
                {"error": "Direct agent connections are off. Connect through a relay."},
                status=403,
            )
        request.vigil_relay_id = None
        return self.get_response(request)

    @staticmethod
    def _relay_id(raw: str):
        """The uuid of an enabled remote relay, or None."""
        try:
            relay_id = uuid.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            return None
        from .models import Relay

        if Relay.objects.filter(pk=relay_id, kind=Relay.Kind.REMOTE,
                                enabled=True).exists():
            return relay_id
        return None
