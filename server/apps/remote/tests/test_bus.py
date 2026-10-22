"""The Redis bus keeps one async client per event loop.

``async_to_sync`` runs each call to an async view on a loop of its own, and a
client whose pool was made on a loop that has since closed fails with "Event
loop is closed" the next time it is used. ``from_url`` connects lazily, so
none of this needs a Redis to talk to.
"""

import asyncio

from django.test import SimpleTestCase

from ..bus import RedisBus


class RedisBusClientTests(SimpleTestCase):
    def test_one_loop_keeps_its_client(self):
        bus = RedisBus()

        async def twice():
            return bus._client(), bus._client()

        first, second = asyncio.run(twice())
        self.assertIs(first, second)

    def test_a_new_loop_gets_a_new_client(self):
        bus = RedisBus()

        async def one():
            return bus._client()

        self.assertIsNot(asyncio.run(one()), asyncio.run(one()))
