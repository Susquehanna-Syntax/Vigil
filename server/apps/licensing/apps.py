from django.apps import AppConfig


class LicensingConfig(AppConfig):
    name = "apps.licensing"
    verbose_name = "Licensing"

    # No ready() work on purpose: the license is loaded lazily on first use
    # (vigil.licensing.current_state) because touching the DB during app
    # startup breaks migrations and management commands. The instance UUID
    # is likewise created on first read, not at boot.
