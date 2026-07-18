from django.apps import AppConfig


class BaselinesConfig(AppConfig):
    name = "apps.baselines"
    verbose_name = "Baselines"

    def ready(self):
        wire()


def wire():
    """Idempotent hook subscription (named handler, so re-wiring after a
    test's hooks.clear() adds nothing twice)."""
    from vigil import hooks

    hooks.subscribe("host_approved", _on_host_approved)


def _on_host_approved(host=None, **_):
    from .models import dispatch_to_host

    if host is not None and getattr(host, "mode", None) != "monitor":
        dispatch_to_host(host)
