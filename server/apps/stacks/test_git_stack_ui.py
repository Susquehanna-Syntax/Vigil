"""QA-23: the stack editor can take its compose file from a Git repository.

The repository is the source of truth for such a stack, so the editor's job is
to show it read-only, offer Pull, and carry the .env side. These read the
shipped files as text: the points worth guarding (read-only, no secret in
markup, the Git endpoints actually called) are all visible in the source.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


def _text(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


class GitSourceInEditorTests(SimpleTestCase):
    def test_template_offers_a_git_source(self):
        page = _text("templates/pages/_stack_editor.html")
        for marker in ('data-sted-source="git"', 'id="sted-git-url"', 'id="sted-git-branch"',
                       'id="sted-git-pin"', 'id="sted-git-path"', 'id="sted-git-cred"',
                       'data-sted-git-pull'):
            self.assertIn(marker, page)

    def test_save_and_pull_use_the_git_api(self):
        js = _text("static/js/vigil-stacks.js")
        for needle in ("git: {", "/pull/", "/api/v1/stacks/git-credentials/", "readOnly"):
            self.assertIn(needle, js)

    def test_secret_never_enters_markup(self):
        js = _text("static/js/vigil-stacks.js")
        offenders = re.findall(r"\$\{[^}]*secret[^}]*\}", js)
        self.assertEqual(offenders, [],
                         f"a credential secret is interpolated into markup: {offenders}")
        self.assertIn(".value = ''", js)

    def test_git_commit_shown_in_revisions(self):
        js = _text("static/js/vigil-stacks.js")
        self.assertIn("git_commit", js)

    def test_credential_names_its_server_and_attaching_asks_totp(self):
        js = _text("static/js/vigil-stacks.js")
        self.assertIn("git_host: gitHost", js)
        self.assertIn("body.git && body.git.credential_id", js)
        self.assertIn("data-sted-git-ref", _text("templates/pages/_stack_editor.html"))
