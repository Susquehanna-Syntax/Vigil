"""The flatpak update check costs about 500 MB for a moment. It is not run
when there is nothing to update or when the host could not spare the memory
(found by `vigil-agent measure`, sprint M8)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import software

GIB = 1024 ** 3
FIREFOX = {"source": "flatpak", "id": "org.mozilla.firefox", "name": "Firefox",
           "version": "131.0", "latest": None}


class FlatpakMemoryTests(unittest.TestCase):
    def collect(self, *, installed, available):
        ran = []

        def fake_run(argv, parse, errors, key, shape=software._LIST_SHAPE):
            ran.append(key)
            if key == "flatpak":
                return [dict(i) for i in installed]
            return {"org.mozilla.firefox": "132.0"}

        errors: dict[str, str] = {}
        with mock.patch.object(software, "_run_and_parse", fake_run), \
                mock.patch.object(software, "_available_memory", lambda: available):
            items = software._collect_flatpak(errors)
        return items, errors, ran

    def test_with_memory_to_spare_updates_are_looked_up(self):
        items, errors, ran = self.collect(installed=[FIREFOX], available=4 * GIB)
        self.assertEqual(ran, ["flatpak", "flatpak-updates"])
        self.assertEqual(errors, {})
        self.assertIn("132.0", str(items[0]))

    def test_a_host_short_of_memory_skips_the_lookup_and_says_so(self):
        items, errors, ran = self.collect(installed=[FIREFOX], available=300 * 1048576)
        self.assertEqual(ran, ["flatpak"])
        self.assertEqual(len(items), 1, "the installed list is still reported")
        self.assertIn("skipped", errors["flatpak-updates"])
        self.assertIn("300 MB", errors["flatpak-updates"])

    def test_nothing_installed_means_nothing_to_look_up(self):
        items, errors, ran = self.collect(installed=[], available=64 * GIB)
        self.assertEqual((items, errors, ran), ([], {}, ["flatpak"]))

    def test_when_memory_cannot_be_read_the_lookup_still_runs(self):
        _items, errors, ran = self.collect(installed=[FIREFOX], available=None)
        self.assertEqual(ran, ["flatpak", "flatpak-updates"])
        self.assertEqual(errors, {})


if __name__ == "__main__":
    unittest.main()
