"""Rotate the task-signing key without re-enrolling the fleet (sprint M3 phase 3).

    1. Set VIGIL_SIGNING_KEY_NEXT_SEED to a new seed and restart.
       Agents learn the next key at their next check-in, signed by the
       current one, and acknowledge it.
    2. manage.py signing_key status      who has acknowledged
    3. manage.py signing_key rotate      switch, once every recently seen
                                         approved host has acknowledged
    4. Later, at leisure: move the new seed into VIGIL_SIGNING_KEY_SEED and
       clear VIGIL_SIGNING_KEY_NEXT_SEED. Which key signs is recorded by its
       public key, so this changes nothing.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from vigil import signing

#: Hosts not seen for this long are not waited for: an agent that comes back
#: after a switch it missed needs re-enrolling, and `status` names them.
RECENT_DAYS = 7


def _approved_recent():
    from apps.hosts.models import Host

    since = timezone.now() - timedelta(days=RECENT_DAYS)
    return (Host.objects.filter(status__in=[Host.Status.ONLINE, Host.Status.OFFLINE],
                                last_checkin__gte=since)
            .order_by("hostname"))


class Command(BaseCommand):
    help = "Show or switch the task-signing key (status, rotate)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("status", help="the active key, the next key and who has acknowledged it")
        rot = sub.add_parser("rotate", help="switch to the next key")
        rot.add_argument("--force", action="store_true",
                         help="switch although some recently seen hosts have not acknowledged")
        rot.add_argument("--note", default="", help="kept with the switch in the key history")

    def handle(self, *args, action, **opts):
        return self.status() if action == "status" else self.rotate(opts["force"], opts["note"])

    def _waiting(self, next_pub):
        return [h for h in _approved_recent() if h.signing_key_ack != next_pub]

    def status(self):
        from apps.hosts.models import SigningKeyEpoch

        self.stdout.write(f"Active key: {signing.get_public_key_b64()}")
        nxt = signing.next_signing_key()
        if nxt is None:
            self.stdout.write("Next key:   none on offer (VIGIL_SIGNING_KEY_NEXT_SEED unset, "
                              "or it holds a key that is or was active)")
        else:
            pub = signing.public_b64(nxt)
            hosts = list(_approved_recent())
            waiting = [h for h in hosts if h.signing_key_ack != pub]
            self.stdout.write(f"Next key:   {pub}")
            self.stdout.write(f"Acknowledged by {len(hosts) - len(waiting)} of {len(hosts)} approved "
                              f"hosts seen in the last {RECENT_DAYS} days.")
            for h in waiting:
                self.stdout.write(f"  waiting: {h.hostname} (last check-in {h.last_checkin:%Y-%m-%d %H:%M})")
        history = list(SigningKeyEpoch.objects.all()[:10])
        if history:
            self.stdout.write("History, newest first:")
            for e in history:
                self.stdout.write(f"  {e.effective_at:%Y-%m-%d %H:%M:%S} {e.public_key} {e.note}")

    def rotate(self, force, note):
        nxt = signing.next_signing_key()
        if nxt is None:
            raise CommandError("No next key on offer: set VIGIL_SIGNING_KEY_NEXT_SEED to a new "
                               "seed, restart, and let the agents check in.")
        waiting = self._waiting(signing.public_b64(nxt))
        if waiting and not force:
            names = ", ".join(h.hostname for h in waiting[:20])
            raise CommandError(
                f"{len(waiting)} recently seen host(s) have not acknowledged the next key yet: "
                f"{names}. They would stop accepting tasks. Wait for their next check-in, or "
                f"pass --force and re-enrol them afterwards.")
        epoch = signing.switch_to_next(note=note)
        self.stdout.write(self.style.SUCCESS(
            f"Switching to {epoch.public_key} at {epoch.effective_at:%H:%M:%S} "
            f"({signing.SWITCH_DELAY_SECONDS} s from now, so every worker changes together)."))
        if waiting:
            self.stdout.write(self.style.WARNING(
                "Forced: these hosts will refuse tasks until re-enrolled: "
                + ", ".join(h.hostname for h in waiting)))
