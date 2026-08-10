"""The catalog lists only images that can genuinely be pulled.

An entry that cannot fetch is worse than no entry: it fails at the moment the
operator has already committed to it. RHEL proper and Windows are deliberately
absent — neither has an anonymous URL with a published digest — and reach the
library through the upload path instead.
"""
import re

from django.test import TestCase

from .catalog import CATALOG, get_entry
from .models import OSImage


class CatalogTests(TestCase):
    def test_it_is_not_empty(self):
        self.assertTrue(CATALOG)

    def test_ids_are_unique(self):
        ids = [e["id"] for e in CATALOG]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_family_is_a_real_osimage_family(self):
        valid = {c[0] for c in OSImage.Family.choices}
        for e in CATALOG:
            self.assertIn(e["os_family"], valid, e["id"])

    def test_every_entry_has_the_required_keys(self):
        required = {"id", "name", "os_family", "version", "architecture",
                    "url", "sha256", "size_bytes"}
        for e in CATALOG:
            self.assertEqual(required - set(e), set(), e["id"])

    def test_every_digest_is_a_sha256(self):
        for e in CATALOG:
            self.assertRegex(e["sha256"], r"^[0-9a-f]{64}$", e["id"])

    def test_every_url_is_https(self):
        """A catalog URL is one we chose; there is no excuse for plain HTTP."""
        for e in CATALOG:
            self.assertTrue(e["url"].startswith("https://"), e["id"])

    def test_sizes_are_plausible(self):
        for e in CATALOG:
            self.assertGreater(e["size_bytes"], 100 * 1024 * 1024, e["id"])

    def test_server_variants_are_present(self):
        """This is a fleet tool — server images are the point."""
        names = " ".join(e["name"].lower() for e in CATALOG)
        self.assertIn("server", names)

    def test_get_entry_finds_and_misses(self):
        self.assertIsNotNone(get_entry(CATALOG[0]["id"]))
        self.assertIsNone(get_entry("no-such-image"))

    def test_windows_and_rhel_are_absent(self):
        """Neither can be fetched anonymously with a published digest. They
        reach the library by upload; a catalog entry would be a promise the
        code cannot keep."""
        ids = " ".join(e["id"] for e in CATALOG).lower()
        self.assertNotIn("windows", ids)
        self.assertNotIn("rhel", ids)
