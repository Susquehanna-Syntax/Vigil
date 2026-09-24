"""Wording contract: the retired separate-repo edition model stays gone.

These tests pin the prose in the contributing docs, the core modules and the
reference app to the current model (one repo, Business code in
``server/apps_business/``), so a rewrite describing the retired separate-repo
model fails the suite.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def _stale_phrases() -> tuple[str, ...]:
    # Built from parts so this file's own guard list never contains the
    # literal stale wording (this file must stay clean for its own grep).
    pro = "Pro"
    ent = "Enterprise"
    return (
        "Vigil-" + pro,
        "Vigil-" + ent,
        f"{pro} / {ent}",
        f"{pro}/{ent}",
    )


_TARGETS = (
    ".github/CONTRIBUTING.md",
    ".github/pull_request_template.md",
    "server/vigil/hooks.py",
    "server/vigil/urls.py",
)


def _repo_root() -> Path:
    return Path(settings.BASE_DIR).parent


def _target_files() -> list[Path]:
    root = _repo_root()
    files = [root / rel for rel in _TARGETS]
    example_dir = root / "server" / "apps" / "example_extension"
    files.extend(
        p
        for p in sorted(example_dir.glob("*.py"))
        if p.name != "test_edition_wording.py"
    )
    return files


class EditionWordingTests(SimpleTestCase):
    def test_no_file_describes_separate_edition_repos(self):
        for path in _target_files():
            text = path.read_text(encoding="utf-8").lower()
            for phrase in _stale_phrases():
                self.assertFalse(
                    phrase.lower() in text,
                    f"{path} still describes the retired edition model ({phrase!r})",
                )

    def test_contributing_states_the_inbound_licence(self):
        text = (_repo_root() / ".github" / "CONTRIBUTING.md").read_text(
            encoding="utf-8"
        )
        for literal in ("Apache-2.0", "Signed-off-by", "AGPL"):
            self.assertIn(literal, text, f"CONTRIBUTING.md is missing {literal!r}")

    def test_the_pr_template_asks_for_a_sign_off(self):
        text = (_repo_root() / ".github" / "pull_request_template.md").read_text(
            encoding="utf-8"
        )
        for literal in ("git commit -s", "Apache-2.0"):
            self.assertIn(literal, text, f"PR template is missing {literal!r}")

    def test_the_readme_links_the_editions_doc(self):
        text = (_repo_root() / "README.md").read_text(encoding="utf-8")
        for literal in ("## Editions", "docs/EDITIONS.md"):
            self.assertIn(literal, text, f"README.md is missing {literal!r}")
