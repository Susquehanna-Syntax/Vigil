from django.apps import AppConfig


class JackilConfig(AppConfig):
    name = "apps.jackil"
    label = "jackil"
    verbose_name = "Jackil integration"

    def ready(self):
        wire()


def wire():
    """Subscribe to the alert lifecycle. Idempotent (named handlers), so tests
    can re-wire after a hooks.clear()."""
    from vigil import hooks

    hooks.subscribe("alert_sent", _on_alert_sent)
    hooks.subscribe("alert_resolved", _on_alert_resolved)


def _on_alert_sent(alert=None, **_):
    if alert is None:
        return
    from .service import on_alert_sent
    on_alert_sent(alert)


def _on_alert_resolved(alert=None, **_):
    if alert is None:
        return
    from .service import on_alert_resolved
    on_alert_resolved(alert)
