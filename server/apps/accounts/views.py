from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import UserProfile
from .totp import generate_secret, generate_totp, otpauth_uri, verify_totp


def _get_or_create_profile(user) -> UserProfile:
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def totp_status(request):
    profile = _get_or_create_profile(request.user)
    return Response({
        "enrolled": bool(profile.totp_confirmed_at and profile.totp_secret),
        "pending": bool(profile.totp_secret and not profile.totp_confirmed_at),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def totp_enroll_start(request):
    """Generate a fresh secret and hand back the provisioning URI.

    Overwrites any pending (unconfirmed) secret so the user can re-scan a
    fresh QR code if they lose the first one. Refuses to clobber an already-
    confirmed secret — the user must ``disable`` first.
    """
    profile = _get_or_create_profile(request.user)
    if profile.totp_confirmed_at and profile.totp_secret:
        return Response({"error": "TOTP is already enrolled. Disable it first."}, status=400)

    secret = generate_secret()
    profile.totp_secret = secret
    profile.totp_confirmed_at = None
    profile.save(update_fields=["totp_secret", "totp_confirmed_at"])

    return Response({
        "secret": secret,
        "otpauth_uri": otpauth_uri(secret, request.user.get_username()),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def totp_enroll_confirm(request):
    code = (request.data.get("code") or "").strip()
    profile = _get_or_create_profile(request.user)
    if not profile.totp_secret:
        return Response({"error": "No enrollment in progress"}, status=400)
    if not verify_totp(profile.totp_secret, code):
        return Response({"error": "Invalid code — check your authenticator clock"}, status=400)
    profile.totp_confirmed_at = now()
    profile.save(update_fields=["totp_confirmed_at"])
    return Response({"enrolled": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def totp_disable(request):
    """Disable TOTP. Requires a valid current code to prove possession."""
    profile = _get_or_create_profile(request.user)
    code = (request.data.get("code") or "").strip()
    if not profile.totp_confirmed_at or not profile.totp_secret:
        return Response({"error": "TOTP is not enrolled"}, status=400)
    if not verify_totp(profile.totp_secret, code):
        return Response({"error": "Invalid code"}, status=400)
    profile.totp_secret = ""
    profile.totp_confirmed_at = None
    profile.save(update_fields=["totp_secret", "totp_confirmed_at"])
    return Response({"enrolled": False})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def totp_debug_code(request):
    """Return the current TOTP code for the authenticated user.

    DEBUG-only helper so local tests can confirm a deploy without pulling out
    a phone. Only available when DEBUG is on.
    """
    from django.conf import settings
    if not settings.DEBUG:
        return Response({"error": "Not available"}, status=404)
    profile = _get_or_create_profile(request.user)
    if not profile.totp_secret:
        return Response({"error": "No secret"}, status=400)
    return Response({"code": generate_totp(profile.totp_secret)})
