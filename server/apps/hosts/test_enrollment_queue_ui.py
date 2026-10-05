"""The wizard and the enrolment queue name the host a re-enrolment replaces.

Phase 03 made approval adopt the existing record; these tests pin the two
surfaces that tell the admin that is what will happen: the enrolment queue on
the settings page (server-rendered) and the wizard's detected-agent step
(plain JS reading the ``replaces`` key from ``check_pending``).
"""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

JS = Path(__file__).resolve().parents[2] / "static" / "js" / "vigil-enroll.js"


class EnrollmentQueueAnnotationTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("op", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.client.force_login(user)

    def test_the_queue_names_the_host_that_will_be_replaced(self):
        online = Host.objects.create(
            hostname="existing-host",
            agent_token="a" * 32 + "0",
            machine_id="mid-1",
            status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED,
        )
        Host.objects.create(
            hostname="re-enrolling",
            agent_token="b" * 32 + "1",
            machine_id="mid-1",
            status=Host.Status.PENDING,
            mode=Host.Mode.MANAGED,
        )
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            f"· replaces {online.hostname}" in resp.content.decode(),
            "queue did not name the replaced host",
        )

    def test_the_queue_says_nothing_for_a_first_enrolment(self):
        Host.objects.create(
            hostname="first-host",
            agent_token="c" * 32 + "2",
            machine_id="mid-2",
            status=Host.Status.PENDING,
            mode=Host.Mode.MANAGED,
        )
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertFalse("· replaces" in html, "queue annotated a first enrolment")


class EnrollmentWizardReplacesTests(SimpleTestCase):
    def test_the_wizard_reads_the_replaces_field(self):
        js = JS.read_text()
        self.assertIn("data.replaces", js)
        self.assertIn("getElementById('enroll-replaces')", js)
        self.assertIn("rep.textContent", js)

    def test_the_wizard_escapes_the_replaced_hostname(self):
        js = JS.read_text()
        self.assertIn("textContent", js)
        self.assertNotIn("innerHTML", js)
