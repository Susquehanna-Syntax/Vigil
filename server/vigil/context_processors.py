from vigil import scoping


def scope_banners(request):
    """A banner for instance admins when a background path read site data
    with no scope and got nothing back (decisions-06 item 17)."""
    try:
        user = getattr(request, "user", None)
        if not scoping.is_instance_admin(user):
            return {"scope_banners": []}
        alert = scoping.scope_alert()
        if not alert:
            return {"scope_banners": []}
        return {"scope_banners": [{
            "severity": "critical",
            "message": (f"A background job read {alert['model']} rows without naming a site scope "
                        f"and got nothing back ({alert['where']}, {alert['at'][:16].replace('T', ' ')}). "
                        "Monitoring may be incomplete until it is fixed; please report this."),
        }]}
    except Exception:  # noqa: BLE001 — a banner failure must never 500 a page
        return {"scope_banners": []}
