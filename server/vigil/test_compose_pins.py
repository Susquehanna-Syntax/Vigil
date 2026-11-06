"""The published compose file pins every image (sprint M3 phase 2).

A floating tag (`latest`, `7-alpine`, `latest-pg16`) means two installs from
the same file run different software, and a TimescaleDB jump can arrive on an
ordinary restart. Dependabot opens the bump pull requests instead. The Vigil
image is pinned to this release, so the compose file in a release tag starts
that release.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

COMPOSE = Path(settings.BASE_DIR).parent / "docker-compose.yml"
EXACT = re.compile(r"^\d+(\.\d+)+(-[a-z0-9]+)?$")   # 2.31.0-pg16, 7.4.12-alpine, 2026.10.7


class ComposePinTests(SimpleTestCase):
    def images(self):
        return re.findall(r"(?m)^\s+image:\s*(\S+)\s*$", COMPOSE.read_text())

    def test_every_image_has_an_exact_tag(self):
        images = self.images()
        self.assertGreater(len(images), 5)
        for image in images:
            name, _, tag = image.rpartition(":")
            if name == "susquehannasyntax/vigil":
                continue
            self.assertRegex(tag, EXACT, image)

    def test_vigil_is_pinned_to_this_release(self):
        vigil = {i for i in self.images() if i.startswith("susquehannasyntax/vigil:")}
        self.assertEqual(vigil, {f"susquehannasyntax/vigil:{settings.VIGIL_VERSION}"})
