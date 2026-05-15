import os

from django.apps import AppConfig


class HostsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hosts"
    verbose_name = "Hosts"

    def ready(self):
        # Validate VIGIL_SIGNING_KEY_SEED at startup. Without this, a malformed
        # seed silently 500s every /api/v1/checkin response — agents retry
        # forever, no tasks dispatch, and nothing surfaces in the UI. Failing
        # here makes the misconfig visible in `docker compose logs` immediately.
        # Set VIGIL_SKIP_STARTUP_CHECKS=1 to bypass (e.g. running collectstatic
        # in a build step that doesn't have the seed yet).
        if os.environ.get("VIGIL_SKIP_STARTUP_CHECKS") == "1":
            return
        from vigil.signing import get_signing_key
        get_signing_key()
