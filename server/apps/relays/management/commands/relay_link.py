"""Hold a link open to every enabled remote relay, for as long as the container runs."""

import asyncio
import logging

from django.core.management.base import BaseCommand

from apps.relays.link import LinkManager

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ("Keep one outbound WebSocket uplink per enabled remote relay, forward "
            "each agent request the relay pushes down it to this web service, and "
            "send the answer back.")

    def handle(self, *args, **options):
        log.info("relay link starting")
        try:
            asyncio.run(LinkManager().run())
        except KeyboardInterrupt:
            pass
