"""The paths an agent may call through the relay, and nothing else.

Two copies of these tuples exist: here, and ``server/apps/relays/trust.py`` on
the dashboard. ``tests/test_paths.py`` parses that file with ``ast`` and fails if
they differ, so the relay's forward allowlist can never drift from the
dashboard's agent-path check.
"""

AGENT_PATHS = ("/api/v1/register", "/api/v1/checkin", "/api/v1/tasks/result/", "/api/v1/reprovision/enroll",
               "/api/v1/agent/wake",
               "/agent/install.sh", "/agent/install.ps1", "/agent/uninstall.sh", "/agent/uninstall.ps1")
AGENT_PREFIXES = ("/api/v1/agent/", "/agent/download/", "/agent/enroll-config/")

WS_PREFIXES = ("/ws/agent/",)

_METHODS = ("GET", "POST", "HEAD")


def allowed(method: str, path: str) -> bool:
    """True for an agent-facing (method, path) pair. Everything else is a 404,
    which keeps the dashboard UI unreachable through the relay."""
    if method.upper() not in _METHODS:
        return False
    # aiohttp hands us the decoded path, so "%2e%2e" is already ".." here; a
    # "%" left over means double encoding, which nothing an agent sends needs.
    if ".." in path or any(c in path for c in "%\\@"):
        return False
    return path in AGENT_PATHS or path.startswith(AGENT_PREFIXES)


def split_tenant(path: str, tenants):
    """Strip a ``/t/<slug>`` prefix. Returns ``(tenant, path)``; tenant is None
    when no tenant owns the request."""
    if path.startswith("/t/"):
        rest = path[len("/t/"):]
        slug, _, tail = rest.partition("/")
        if slug in tenants:
            return slug, "/" + tail.lstrip("/") if tail else "/"
    # A /t/<slug> prefix that no tenant owns is nobody's request.
    if path.startswith("/t/"):
        return None, path
    if "default" in tenants:
        return "default", path
    return None, path
