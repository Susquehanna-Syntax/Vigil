"""Email through the SMTP settings entered in the UI (or the environment)."""
from . import ChannelError, ChannelType, Field, Message, register


@register
class Email(ChannelType):
    kind = "email"
    label = "Email"
    fields = (
        Field("recipients", "Recipients", kind="list",
              help="One address per line, or separated by commas."),
    )

    def send(self, config: dict, message: Message) -> None:
        from django.core.mail import send_mail

        from apps.instance.config import mail_connection, setting

        p = message.payload
        event = message.event.upper()
        severity = message.severity.upper()
        subject = f"[Vigil {event}] [{severity}] {message.host}: {message.text}"
        lines = [f"Host: {message.host}", f"Severity: {severity}", f"Event: {event}",
                 f"Message: {message.text}"]
        if message.site:
            lines.append(f"Site: {message.site}")
        lines += [f"Metric Value: {p.get('metric_value')}", f"Rule: {p.get('rule')}",
                  f"Fired At: {p.get('fired_at')}"]
        if p.get("resolved_at"):
            lines.append(f"Resolved At: {p['resolved_at']}")
        if message.link:
            lines.append(f"Open in Vigil: {message.link}")
        try:
            # The connection is built explicitly rather than left to Django's
            # settings lookup, so SMTP entered in the UI is actually used.
            send_mail(subject=subject, message="\n".join(lines) + "\n",
                      from_email=setting("VIGIL_NOTIFICATION_FROM_EMAIL"),
                      recipient_list=config["recipients"], connection=mail_connection())
        except Exception as exc:  # noqa: BLE001 — SMTP errors come in many classes
            raise ChannelError(f"the mail server refused it ({type(exc).__name__})") from exc
