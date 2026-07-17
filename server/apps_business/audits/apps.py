from django.apps import AppConfig


class AuditsConfig(AppConfig):
    name = "apps_business.audits"
    label = "business_audits"
    verbose_name = "Audit log (Business)"

    def ready(self):
        wire()


def wire():
    """Subscribe the audit handlers. Idempotent (named handlers + dispatch
    uids), so tests can re-wire after a hooks.clear(). Recording is
    unconditional (see models.AuditEvent docstring); the license gates the
    viewer, not the writer."""
    from django.contrib.auth.signals import (
        user_logged_in, user_logged_out, user_login_failed,
    )

    from vigil import hooks

    hooks.subscribe("host_approved", _on_host_approved)
    hooks.subscribe("host_rejected", _on_host_rejected)
    hooks.subscribe("alert_fired", _on_alert_fired)
    hooks.subscribe("task_completed", _on_task_completed)

    user_logged_in.connect(_on_login, dispatch_uid="business_audits.login")
    user_logged_out.connect(_on_logout, dispatch_uid="business_audits.logout")
    user_login_failed.connect(_on_login_failed,
                              dispatch_uid="business_audits.login_failed")


def _on_host_approved(host=None, approved_by=None, **_):
    from .models import record
    record("host.approved", user=approved_by, target=getattr(host, "hostname", ""))


def _on_host_rejected(host=None, rejected_by=None, **_):
    from .models import record
    record("host.rejected", user=rejected_by, target=getattr(host, "hostname", ""))


def _on_alert_fired(alert=None, **_):
    from .models import record
    record("alert.fired", target=str(alert))


def _on_task_completed(task=None, **_):
    from .models import record
    record("task.completed", target=str(task))


def _client_ip(request):
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR") or None


def _on_login(sender, request=None, user=None, **_):
    from .models import record
    record("auth.login", user=user, auth_method="session", ip=_client_ip(request))


def _on_logout(sender, request=None, user=None, **_):
    from .models import record
    record("auth.logout", user=user, auth_method="session", ip=_client_ip(request))


def _on_login_failed(sender, credentials=None, request=None, **_):
    from .models import record
    record("auth.login_failed",
           target=(credentials or {}).get("username", "")[:150],
           ip=_client_ip(request))
