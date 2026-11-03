from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil import scoping


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def writable_sites(request):
    """The sites a create form may offer this user (scoping.site_for_write)."""
    return Response(scoping.writable_sites(request.user))
