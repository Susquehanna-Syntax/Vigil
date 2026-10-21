"""The shared secret between a relay container and this dashboard's link.

The relay container (QA-53) forwards agent requests to the dashboard over the
WebSocket uplink held open by ``vigil-link`` (QA-54). The link stamps every
forwarded request with ``X-Vigil-Link`` so the dashboard can tell a relayed
agent request from one that walked straight in off the LAN — the difference
between a trustworthy ``X-Vigil-Agent-IP`` and a forged one.

The token is derived from ``SECRET_KEY``, never stored, so it cannot be read out
of the database; an attacker who owns the DB still cannot attest an IP.
"""

import hashlib
import hmac

from django.conf import settings

LINK_HEADER = "HTTP_X_VIGIL_LINK"
RELAY_HEADER = "HTTP_X_VIGIL_RELAY"
AGENT_IP_HEADER = "HTTP_X_VIGIL_AGENT_IP"

#: Every path an agent calls, exactly (the relay's forward allowlist is the same
#: list plus the health check, which the relay answers itself).
AGENT_PATHS = (
    "/api/v1/register",
    "/api/v1/checkin",
    "/api/v1/tasks/result/",
    "/api/v1/reprovision/enroll",
    "/agent/install.sh",
    "/agent/install.ps1",
    "/agent/uninstall.sh",
    "/agent/uninstall.ps1",
    "/agent/info/",
)
AGENT_PREFIXES = ("/api/v1/agent/", "/agent/download/")


def is_agent_path(path: str) -> bool:
    return path in AGENT_PATHS or path.startswith(AGENT_PREFIXES)


def link_token() -> str:
    return hmac.new(settings.SECRET_KEY.encode(), b"vigil-link-v1",
                    hashlib.sha256).hexdigest()


def from_link(request) -> bool:
    return hmac.compare_digest(request.META.get(LINK_HEADER, ""), link_token())
