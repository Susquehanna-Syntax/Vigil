from django.apps import AppConfig


class PlaybooksConfig(AppConfig):
    name = "apps.playbooks"
    verbose_name = "Playbooks"
    # Pinned to the app's original label. Every existing django_migrations row,
    # and every historical FK string in another app's migrations, says
    # "baselines"; renaming the label would orphan all of them and try to
    # re-create tables that already hold a live deployment's data. The label is
    # invisible to users, so it keeps the old name and everything else moves.
    label = "baselines"

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
