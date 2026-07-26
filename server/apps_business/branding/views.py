"""Branding API — reads open (any authed user can theme), writes gated by
``require_feature("branding")``: 402 + upgrade body. Admin-only for writes."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin

from .models import BrandingConfig
from .serializers import BrandingConfigSerializer


def _gate(request):
    """The write gate. Returns a 402 Response, or None when licensed."""
    from rest_framework.exceptions import APIException

    from vigil.licensing import require_feature

    perm = require_feature("branding")()
    try:
        perm.has_permission(request, None)
    except APIException as exc:
        return Response(exc.detail, status=402)
    return None


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def branding_config(request):
    if request.method == "GET":
        return Response(BrandingConfigSerializer(BrandingConfig.load()).data)

    # PATCH path: license gate first, then admin check
    denied = _gate(request)
    if denied is not None:
        return denied

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": IsAdmin.message}, status=403)

    obj = BrandingConfig.load()
    ser = BrandingConfigSerializer(obj, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data)
