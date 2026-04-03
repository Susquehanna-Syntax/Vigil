from rest_framework.response import Response

from .models import Host


def authenticate_agent(request):
    """Validate a Bearer agent token.

    Returns (Host, None) on success or (None, error Response) on failure.
    """
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None, Response({"error": "Agent token required"}, status=401)
    token = header[7:].strip()
    try:
        return Host.objects.get(agent_token=token), None
    except Host.DoesNotExist:
        return None, Response({"error": "Invalid agent token"}, status=401)