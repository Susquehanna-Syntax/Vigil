from apps.civilsso import client


def civil(request):
    """Exposes ``civil_enabled`` so login templates can show the SSO button."""
    return {"civil_enabled": client.enabled()}
