from django.apps import AppConfig

EVENTS = ("alert_fired", "host_approved", "host_rejected",
          "task_completed", "insight_created")


class AutomationsConfig(AppConfig):
    name = "apps.automations"
    verbose_name = "Automations"

    def ready(self):
        wire()


def wire():
    """Subscribe the (module-level, so idempotent) handlers to each event."""
    from vigil import hooks

    for event in EVENTS:
        hooks.subscribe(event, _HANDLERS[event])


def _on(event_name):
    def handler(**payload):
        from .engine import handle_event
        handle_event(event_name, payload)
    handler.__name__ = f"automations_on_{event_name}"
    return handler


# Built once at import time — stable identities, so wire() never double-subscribes.
_HANDLERS = {event: _on(event) for event in EVENTS}
