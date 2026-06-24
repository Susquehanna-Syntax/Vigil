from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil.editions import active_edition, enabled_features, feature_enabled


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ping(request):
    """Proves an edition app can mount its own authenticated route under ext/.

    A real Pro/Enterprise app would expose its feature endpoints here.
    """
    return Response({
        "ok": True,
        "edition": active_edition(),
        "features": sorted(enabled_features()),
        "ai_suggestions_enabled": feature_enabled("ai_suggestions"),
    })
