from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tasks.spec import ACTION_REGISTRY


class Command(BaseCommand):
    help = (
        "Emit the action reference (ACTION_REGISTRY) as JSON for the "
        "Vigil-Approved-Scripts community repo. Run after changing "
        "ACTION_REGISTRY to resync references/actions.json."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default=None,
            help="Write the JSON to this file instead of stdout.",
        )

    def handle(self, *args, **opts):
        payload = {
            "vigil_version": settings.VIGIL_VERSION,
            "actions": {
                name: {
                    "label": entry["label"],
                    "risk": entry["risk"],
                    "required": list(entry["required"]),
                    "optional": list(entry["optional"]),
                }
                for name, entry in ACTION_REGISTRY.items()
            },
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if opts["output"]:
            with open(opts["output"], "w", encoding="utf-8") as fh:
                fh.write(text)
            self.stdout.write(
                self.style.SUCCESS(f"Action reference written to {opts['output']}")
            )
        else:
            self.stdout.write(text)
