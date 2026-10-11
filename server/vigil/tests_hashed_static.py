"""The image links content-hashed static files (VIGIL_HASHED_STATIC).

A plain /static/js/vigil-stacks.js let a cache in front of Vigil serve the
2026.14.0 script with the 2026.14.1 page: the old script built its own modal
stack editor, so the new full-screen one never appeared."""
from pathlib import Path

from django.test import SimpleTestCase

from vigil.storage import HashedStaticStorage


class HashedStaticTests(SimpleTestCase):
    def test_the_image_turns_hashed_names_on_before_collectstatic(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        flag = dockerfile.index("ENV VIGIL_HASHED_STATIC=1")
        self.assertLess(flag, dockerfile.index("manage.py collectstatic"))

    def test_source_map_comments_are_left_alone(self):
        """Vendored libraries point at .map files Vigil does not ship."""
        patterns = dict(HashedStaticStorage.patterns)
        flat = [p if isinstance(p, str) else p[0] for pats in patterns.values() for p in pats]
        self.assertFalse([p for p in flat if "sourceMappingURL" in p])
        self.assertTrue(any("url\\(" in p for p in flat), "CSS url() is still rewritten")
