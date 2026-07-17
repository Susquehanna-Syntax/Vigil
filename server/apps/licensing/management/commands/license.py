"""``manage.py license`` — the CLI path for license input (spec §4 input #2).

Subcommands::

    manage.py license show          # state, instance UUID, seats, features
    manage.py license set <blob>    # store + reload, no restart needed
    manage.py license clear         # remove the stored (DB) license

``VIGIL_LICENSE_KEY`` (env) always wins over the stored row.
"""

from django.core.management.base import BaseCommand, CommandError

from vigil import licensing


class Command(BaseCommand):
    help = "Show or set this deployment's Vigil license."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("show")
        setp = sub.add_parser("set")
        setp.add_argument("blob")
        sub.add_parser("clear")

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "set":
            state = licensing.set_license(opts["blob"])
            if state.status is licensing.Status.INVALID:
                raise CommandError(f"stored, but does not verify: {state.detail}")
            self.stdout.write(self.style.SUCCESS(f"license {state.status.value}"))
        elif action == "clear":
            licensing.set_license("")
            self.stdout.write("stored license cleared")
        self._show()

    def _show(self):
        state = licensing.current_state()
        self.stdout.write(f"instance:  {licensing.instance_id()}")
        self.stdout.write(f"status:    {state.status.value} (tier: {state.tier})")
        if state.source:
            self.stdout.write(f"source:    {state.source}")
        if state.claims:
            c = state.claims
            self.stdout.write(f"org:       {c.org}")
            self.stdout.write(f"seats:     {licensing.seats_used()}/{c.seats} used")
            self.stdout.write(f"features:  {', '.join(c.features)}")
        else:
            self.stdout.write(f"seats:     {licensing.seats_used()}/{licensing.seats_allowed()} used")
        for b in licensing.banners():
            self.stdout.write(self.style.WARNING(f"[{b['severity']}] {b['message']}"))
