import logging
import os

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class HostsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.hosts"
    verbose_name = "Hosts"

    def ready(self):
        self._warn_if_agent_version_pinned()
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

    @staticmethod
    def _warn_if_agent_version_pinned():
        """Say so when a set VIGIL_AGENT_VERSION is being ignored.

        The variable used to override the expected agent version, and the
        shipped compose file always passed it with a default that nobody
        remembered to bump — so a stale value quietly decided whether the whole
        fleet read as outdated. It is detected from the bundled agent now.
        Leaving the variable in place is harmless, but an operator who set it
        deliberately deserves to hear that it does nothing.
        """
        from django.conf import settings

        if not getattr(settings, "VIGIL_AGENT_VERSION_ENV_IGNORED", False):
            return
        logger.warning(
            "VIGIL_AGENT_VERSION is set (%s) and is being ignored — the "
            "expected agent version is detected from the bundled agent, now "
            "%s. Remove it from your .env / compose file; it no longer does "
            "anything.",
            os.environ.get("VIGIL_AGENT_VERSION"),
            getattr(settings, "VIGIL_AGENT_VERSION", "unknown"),
        )
