"""Branding API — reads open (any authed user can theme), writes gated by
``require_feature("branding")``: 402 + upgrade body. Admin-only for writes."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from vigil.licensing import licence_gate

from .models import BrandingConfig
from .serializers import BrandingConfigSerializer


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def branding_config(request):
    if request.method == "GET":
        return Response(BrandingConfigSerializer(BrandingConfig.load()).data)

    # PATCH path: license gate first, then admin check
    denied = licence_gate(request, "branding")
    if denied is not None:
        return denied

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": IsAdmin.message}, status=403)

    obj = BrandingConfig.load()
    ser = BrandingConfigSerializer(obj, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data)
