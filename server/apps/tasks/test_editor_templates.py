"""The bundled task templates: every one is a valid task, and the API serves them.

Templates are the editor's first impression of the task language, so the bar is
not "the picker shows something" but "the YAML a newcomer copies is a task
Vigil accepts, and it says what it does in a comment". Both halves are cheap to
check and both rot silently otherwise: a renamed action param leaves a template
that fails the moment someone saves it, and a comment block is exactly the thing
a later edit drops.
"""

from pathlib import Path

import yaml
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.tasks.spec import parse_and_validate

TEMPLATES_DIR = Path(__file__).resolve().parent / "editor_templates"
User = get_user_model()


def make_admin(username):
    """An account IsAdmin accepts: the admin role on the profile."""
    user = User.objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=Role.ADMIN)
    return user

# The kept-by-name templates the milestone calls out by id, plus the Learn
# series' size: a lost file is a lost lesson, so the test names them.
REQUIRED_IDS = ("install-trivy", "run-trivy-scan")
LEARN_COUNT = 9


def _index():
    return yaml.safe_load((TEMPLATES_DIR / "index.yaml").read_text())


class EditorTemplateFileTests(SimpleTestCase):
    def test_every_index_entry_exists_and_validates(self):
        for entry in _index():
            with self.subTest(template=entry["id"]):
                path = TEMPLATES_DIR / entry["file"]
                self.assertTrue(path.is_file(), f"{entry['file']} is missing")
                # Raises SpecError on anything the editor would refuse to save.
                parse_and_validate(path.read_text())

    def test_every_template_opens_with_a_comment_block(self):
        for entry in _index():
            with self.subTest(template=entry["id"]):
                text = (TEMPLATES_DIR / entry["file"]).read_text()
                first = text.splitlines()[0] if text.strip() else ""
                self.assertTrue(
                    first.startswith("#"),
                    f"{entry['file']} must open with a # comment block")
                # The block explains the template: at least three comment lines
                # before the YAML starts.
                leading = 0
                for line in text.splitlines():
                    if line.startswith("#") or not line.strip():
                        leading += 1
                    else:
                        break
                self.assertGreaterEqual(leading, 3,
                                        f"{entry['file']} has no comment block")

    def test_no_comment_line_is_wider_than_the_editor(self):
        for entry in _index():
            text = (TEMPLATES_DIR / entry["file"]).read_text()
            for number, line in enumerate(text.splitlines(), start=1):
                if line.startswith("#"):
                    with self.subTest(template=entry["id"], line=number):
                        self.assertLess(len(line), 110)

    def test_index_ids_are_unique_and_files_are_all_listed(self):
        index = _index()
        ids = [e["id"] for e in index]
        self.assertEqual(len(ids), len(set(ids)), "duplicate template id")
        listed = {e["file"] for e in index}
        on_disk = {p.name for p in TEMPLATES_DIR.glob("*.yaml")} - {"index.yaml"}
        self.assertEqual(on_disk - listed, set(),
                         "template files the index does not serve")

    def test_learn_series_has_nine_numbered_entries(self):
        learn = [e for e in _index() if e["group"] == "Learn the task language"]
        self.assertEqual(len(learn), LEARN_COUNT)
        self.assertEqual([e["id"] for e in learn],
                         [f"learn-{n}" for n in range(1, LEARN_COUNT + 1)])
        for number, entry in enumerate(learn, start=1):
            self.assertTrue(entry["label"].startswith(f"Learn {number}:"),
                            f"{entry['id']} label must start with the number")

    def test_kept_templates_are_present(self):
        ids = {e["id"] for e in _index()}
        for required in REQUIRED_IDS:
            self.assertIn(required, ids)

    def test_groups_appear_in_the_documented_order(self):
        seen = []
        for entry in _index():
            if not seen or seen[-1] != entry["group"]:
                seen.append(entry["group"])
        self.assertEqual(seen, ["Learn the task language", "Software",
                                "Services and system", "Containers",
                                "Security scanning"])


class EditorTemplateApiTests(TestCase):
    def setUp(self):
        self.user = make_admin("tpl")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_endpoint_returns_the_templates_in_index_order(self):
        body = self.client.get("/api/v1/tasks/editor-templates/").json()
        self.assertEqual([r["id"] for r in body["results"]],
                         [e["id"] for e in _index()])
        first = body["results"][0]
        self.assertEqual(set(first), {"id", "label", "group", "summary", "yaml"})
        self.assertIn("restart_service", first["yaml"])

    def test_anonymous_is_refused(self):
        self.assertIn(APIClient().get("/api/v1/tasks/editor-templates/").status_code,
                      (401, 403))

    def test_the_editor_no_longer_carries_the_templates_itself(self):
        js = (Path(__file__).resolve().parents[2] / "static" / "js"
              / "vigil-tasks.js").read_text()
        self.assertNotIn("const EDITOR_TEMPLATES", js)
        self.assertIn("/api/v1/tasks/editor-templates/", js)
