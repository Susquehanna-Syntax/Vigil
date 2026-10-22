import os

from django.conf import settings
from django.core.checks import Warning, register

from vigil.license_keys import is_release_build


@register()
def license_key_override_ignored(app_configs, **kwargs):
    """Say so when VIGIL_LICENSE_PUBLIC_KEY is set on a release image, which
    ignores it (vigil/license_keys.py), rather than leave it silently doing
    nothing."""
    if os.environ.get("VIGIL_LICENSE_PUBLIC_KEY") and is_release_build(settings.BASE_DIR):
        return [Warning(
            "VIGIL_LICENSE_PUBLIC_KEY is ignored by release images: licenses are verified "
            "against the key built into this release.",
            hint="Remove it. Internal installs on dev-signed licenses set VIGIL_ACCEPT_DEV_LICENSES=true.",
            id="licensing.W001",
        )]
    return []
