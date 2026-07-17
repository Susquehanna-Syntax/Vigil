from vigil import licensing


def license_banners(request):
    """Banner data for base.html. Empty for anonymous pages (login) — the
    banner bar is dashboard furniture, not a public disclosure."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"license_banners": []}
    try:
        return {"license_banners": licensing.banners()}
    except Exception:  # noqa: BLE001 — a banner failure must never 500 a page
        return {"license_banners": []}
