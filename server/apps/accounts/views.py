from datetime import timedelta

from django.contrib import auth
from django.contrib.auth import authenticate, get_user_model
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import LoginAttempt, UserProfile
from .totp import generate_secret, generate_totp, otpauth_uri, verify_totp

# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------
# Sliding window over failed attempts. Per-username stops a targeted guess
# even when the attacker rotates IPs; per-IP stops spraying many usernames
# from one address. IP comes from REMOTE_ADDR only — never a forwarded
# header — matching how agent checkins derive it.
LOGIN_ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_IP = 20


def _login_blocked(username: str, ip: str | None) -> bool:
    since = now() - LOGIN_ATTEMPT_WINDOW
    recent = LoginAttempt.objects.filter(created_at__gte=since)
    if username and recent.filter(username=username).count() >= MAX_FAILURES_PER_USERNAME:
        return True
    if ip and recent.filter(ip=ip).count() >= MAX_FAILURES_PER_IP:
        return True
    return False


def _record_login_failure(username: str, ip: str | None) -> None:
    LoginAttempt.objects.create(username=username[:150], ip=ip)
    # Opportunistic prune — failures only matter inside the window; keep a
    # day for operator forensics and drop the rest.
    LoginAttempt.objects.filter(created_at__lt=now() - timedelta(hours=24)).delete()


def _clear_login_failures(username: str) -> None:
    LoginAttempt.objects.filter(username=username).delete()


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
    profile.save(update_fields=["totp_secret_encrypted", "totp_confirmed_at"])

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
    profile.save(update_fields=["totp_secret_encrypted", "totp_confirmed_at"])
    return Response({"enrolled": False})


# ---------------------------------------------------------------------------
# Setup / login / logout — HTML views
# ---------------------------------------------------------------------------

def setup_view(request):
    """First-time admin registration with mandatory TOTP enrollment.

    Step 1 (no setup session): create the superuser account.
    Step 2 (setup session present): scan the TOTP QR and confirm a code.

    The account is created in step 1, but the browser is not logged in until a
    TOTP code is confirmed in step 2 — abandoning setup therefore leaves no
    authenticated session behind.
    """
    User = get_user_model()

    # Bounce completed users away from /setup/
    if User.objects.exists() and "setup_totp_secret" not in request.session:
        return redirect("dashboard") if request.user.is_authenticated else redirect("login")

    error = None

    # ── Step 2: TOTP confirmation ────────────────────────────────────────
    if "setup_totp_secret" in request.session:
        pending_user_id = request.session.get("setup_user_id")
        pending_user = (
            User.objects.filter(pk=pending_user_id).first() if pending_user_id else None
        )
        if pending_user is None:
            # Setup session lost track of its user — restart from step 1.
            request.session.pop("setup_totp_secret", None)
            request.session.pop("setup_user_id", None)
            return redirect("setup")

        secret = request.session["setup_totp_secret"]
        if request.method == "POST":
            code = request.POST.get("totp_code", "").strip()
            if verify_totp(secret, code):
                profile, _ = UserProfile.objects.get_or_create(user=pending_user)
                profile.totp_secret = secret
                profile.totp_confirmed_at = now()
                profile.save()
                # Log in only now that TOTP is confirmed.
                auth.login(
                    request,
                    pending_user,
                    backend="django.contrib.auth.backends.ModelBackend",
                )
                request.session.pop("setup_totp_secret", None)
                request.session.pop("setup_user_id", None)
                return redirect("dashboard")
            error = "Invalid code — check your authenticator clock"

        uri = otpauth_uri(secret, pending_user.get_username())
        return render(request, "setup.html", {
            "step": 2,
            "totp_secret": secret,
            "totp_uri": uri,
            "error": error,
        })

    # ── Step 1: Account creation ─────────────────────────────────────────
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        confirm = request.POST.get("confirm", "")

        if not username or not password:
            error = "Username and password are required"
        elif password != confirm:
            error = "Passwords do not match"
        elif len(password) < 8:
            error = "Password must be at least 8 characters"
        else:
            user = User.objects.create_superuser(username=username, password=password)
            secret = generate_secret()
            request.session["setup_user_id"] = user.pk
            request.session["setup_totp_secret"] = secret
            uri = otpauth_uri(secret, username)
            return render(request, "setup.html", {
                "step": 2,
                "totp_secret": secret,
                "totp_uri": uri,
            })

    return render(request, "setup.html", {"step": 1, "error": error})


def login_view(request):
    User = get_user_model()
    if not User.objects.exists():
        return redirect("setup")
    if request.user.is_authenticated:
        return redirect("dashboard")

    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        ip = request.META.get("REMOTE_ADDR")

        if _login_blocked(username, ip):
            error = "Too many failed sign-in attempts. Try again in a few minutes."
            return render(request, "login.html", {"error": error}, status=429)

        user = authenticate(request, username=username, password=password)
        if user is not None:
            _clear_login_failures(username)
            auth.login(request, user)
            # Same-host relative targets only — anything else is an open
            # redirect a phisher could ride out of a legitimate login link.
            next_url = request.GET.get("next", "/")
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = "/"
            return redirect(next_url)

        _record_login_failure(username, ip)
        error = "Invalid username or password"

    return render(request, "login.html", {"error": error})


def logout_view(request):
    auth.logout(request)
    return redirect("login")


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
