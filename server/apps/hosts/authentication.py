from rest_framework.response import Response

from .models import Host


def authenticate_agent(request, allow_unapproved: bool = False):
    """Validate a Bearer agent token.

    Returns (Host, None) on success or (None, error Response) on failure.

    Only an approved host passes unless *allow_unapproved*. Registering is open
    by design, so a token alone proves nothing until an admin approves the host:
    without this, a host nobody approved could fetch registry credentials and
    use every other agent endpoint. Check-in is the one caller that opts out,
    since a pending host has to check in to reach the enrolment queue.
    """
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None, Response({"error": "Agent token required"}, status=401)
    token = header[7:].strip()
    try:
        host = Host.objects.get(agent_token=token)
    except Host.DoesNotExist:
        return None, Response({"error": "Invalid agent token"}, status=401)
    if not allow_unapproved and host.status in (Host.Status.PENDING, Host.Status.REJECTED):
        return None, Response({"error": "This host has not been approved"}, status=403)
    return host, None