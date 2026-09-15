"""Tests for UI-settable instance settings.

The interesting behaviour is not "a form saves a row" — it is the precedence
ladder, and the promise that a secret which goes into the database never comes
back out of the API.
"""

from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.instance import config
from apps.instance.models import InstanceSetting

URL = "/api/v1/instance-settings/"


def _admin(username="admin1"):
    user = get_user_model().objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=Role.ADMIN)
    return user


def _viewer(username="viewer1"):
    user = get_user_model().objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=Role.VIEWER)
    return user


class PrecedenceTests(TestCase):
    def setUp(self):
        config.invalidate()

    def tearDown(self):
        config.invalidate()

    @override_settings(GREENBONE_URL="from-code")
    def test_code_default_when_nothing_set(self):
        self.assertEqual(config.setting("GREENBONE_URL"), "from-code")
        self.assertEqual(config.source("GREENBONE_URL"), "default")

    @override_settings(GREENBONE_URL="from-code")
    def test_db_row_beats_code_default(self):
        config.write("GREENBONE_URL", "gvm.lan:9390")
        self.assertEqual(config.setting("GREENBONE_URL"), "gvm.lan:9390")
        self.assertEqual(config.source("GREENBONE_URL"), "db")

    @override_settings(GREENBONE_URL="from-env")
    def test_env_beats_db_row(self):
        """A GitOps install whose compose file sets the variable must not be
        silently overridden by a row someone typed into the form."""
        config.write("GREENBONE_URL", "gvm.lan:9390")
        with mock.patch.dict(os.environ, {"GREENBONE_URL": "from-env"}):
            self.assertEqual(config.setting("GREENBONE_URL"), "from-env")
            self.assertEqual(config.source("GREENBONE_URL"), "env")
            self.assertTrue(config.env_locked("GREENBONE_URL"))

    @override_settings(GREENBONE_URL="from-code")
    def test_empty_env_variable_is_not_a_lock(self):
        """``GREENBONE_URL=`` left in a compose file is a placeholder, not a
        value; treating it as a lock would disable the UI forever."""
        config.write("GREENBONE_URL", "gvm.lan:9390")
        with mock.patch.dict(os.environ, {"GREENBONE_URL": ""}):
            self.assertFalse(config.env_locked("GREENBONE_URL"))
            self.assertEqual(config.setting("GREENBONE_URL"), "gvm.lan:9390")

    @override_settings(VIGIL_ALERT_RETENTION_DAYS=90, VIGIL_KEV_LIVE_REFRESH=False,
                       VIGIL_DB_SIZE_WARN_GB=20)
    def test_values_come_back_typed(self):
        config.write("VIGIL_ALERT_RETENTION_DAYS", "14")
        config.write("VIGIL_KEV_LIVE_REFRESH", True)
        config.write("VIGIL_DB_SIZE_WARN_GB", "2.5")
        self.assertEqual(config.setting("VIGIL_ALERT_RETENTION_DAYS"), 14)
        self.assertIs(config.setting("VIGIL_KEV_LIVE_REFRESH"), True)
        self.assertEqual(config.setting("VIGIL_DB_SIZE_WARN_GB"), 2.5)

    @override_settings(VIGIL_ALERT_RETENTION_DAYS=90)
    def test_corrupt_stored_value_falls_back(self):
        InstanceSetting.objects.create(key="VIGIL_ALERT_RETENTION_DAYS", value="soon")
        config.invalidate()
        self.assertEqual(config.setting("VIGIL_ALERT_RETENTION_DAYS"), 90)

    @override_settings(GREENBONE_URL="from-code")
    def test_clearing_reverts_to_the_default(self):
        config.write("GREENBONE_URL", "gvm.lan:9390")
        config.write("GREENBONE_URL", "")
        self.assertFalse(InstanceSetting.objects.filter(key="GREENBONE_URL").exists())
        self.assertEqual(config.setting("GREENBONE_URL"), "from-code")

    @override_settings(GREENBONE_URL="from-code")
    def test_a_write_is_visible_to_the_next_read(self):
        """The cache must not outlive a write made in the same process, or
        the settings page would show the old value straight after saving."""
        self.assertEqual(config.setting("GREENBONE_URL"), "from-code")
        config.write("GREENBONE_URL", "gvm.lan:9390")
        self.assertEqual(config.setting("GREENBONE_URL"), "gvm.lan:9390")


class SecretTests(TestCase):
    def setUp(self):
        config.invalidate()
        self.client = APIClient()
        self.client.force_authenticate(_admin())

    def tearDown(self):
        config.invalidate()

    def test_secret_is_encrypted_at_rest(self):
        config.write("GREENBONE_PASSWORD", "hunter2")
        row = InstanceSetting.objects.get(key="GREENBONE_PASSWORD")
        self.assertEqual(row.value, "")
        self.assertNotIn(b"hunter2", bytes(row.value_encrypted))
        self.assertEqual(config.setting("GREENBONE_PASSWORD"), "hunter2")

    def test_secret_never_comes_back_from_the_api(self):
        config.write("GREENBONE_PASSWORD", "hunter2")
        body = self.client.get(URL).json()
        blob = str(body)
        self.assertNotIn("hunter2", blob)
        field = self._field(body, "GREENBONE_PASSWORD")
        self.assertEqual(field["value"], "")
        self.assertTrue(field["is_set"])

    def test_blank_secret_leaves_the_stored_one_alone(self):
        """The form posts back an empty password box when it was not retyped;
        that must not wipe the credential."""
        config.write("GREENBONE_PASSWORD", "hunter2")
        resp = self.client.post(
            URL, {"values": {"GREENBONE_PASSWORD": ""}}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(config.setting("GREENBONE_PASSWORD"), "hunter2")

    def test_clearing_a_secret_is_explicit(self):
        config.write("GREENBONE_PASSWORD", "hunter2")
        resp = self.client.post(
            URL, {"clear": ["GREENBONE_PASSWORD"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(config.is_set("GREENBONE_PASSWORD"))

    @staticmethod
    def _field(body, name):
        for group in body["groups"]:
            for field in group["fields"]:
                if field["name"] == name:
                    return field
        raise AssertionError(f"{name} not in response")


class ApiTests(TestCase):
    def setUp(self):
        config.invalidate()
        self.client = APIClient()

    def tearDown(self):
        config.invalidate()

    def test_viewer_cannot_read_or_write(self):
        self.client.force_authenticate(_viewer())
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(
            self.client.post(URL, {"values": {"EMAIL_HOST": "x"}},
                             format="json").status_code, 403)

    def test_anonymous_cannot_read(self):
        self.assertIn(self.client.get(URL).status_code, (401, 403))

    def test_unknown_key_is_rejected(self):
        self.client.force_authenticate(_admin())
        resp = self.client.post(
            URL, {"values": {"DJANGO_SECRET_KEY": "nope"}}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_bad_timezone_is_rejected(self):
        """A typo here does not look like a bug, it looks like maintenance
        windows that never open."""
        self.client.force_authenticate(_admin())
        resp = self.client.post(
            URL, {"values": {"VIGIL_TIMEZONE": "America/Nowhere"}}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("VIGIL_TIMEZONE", resp.json()["errors"])
        self.assertFalse(InstanceSetting.objects.filter(key="VIGIL_TIMEZONE").exists())

    def test_bad_number_is_rejected(self):
        self.client.force_authenticate(_admin())
        resp = self.client.post(
            URL, {"values": {"VIGIL_ALERT_RETENTION_DAYS": "forever"}}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_one_bad_value_rejects_the_whole_post(self):
        self.client.force_authenticate(_admin())
        resp = self.client.post(URL, {"values": {
            "EMAIL_HOST": "smtp.example.com",
            "VIGIL_TIMEZONE": "Mars/Olympus",
        }}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(InstanceSetting.objects.filter(key="EMAIL_HOST").exists())

    def test_env_locked_key_is_reported_not_written(self):
        self.client.force_authenticate(_admin())
        with mock.patch.dict(os.environ, {"EMAIL_HOST": "smtp.env"}):
            resp = self.client.post(
                URL, {"values": {"EMAIL_HOST": "smtp.ui"}}, format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["env_locked"], ["EMAIL_HOST"])
        self.assertFalse(InstanceSetting.objects.filter(key="EMAIL_HOST").exists())

    def test_get_marks_env_locked_fields(self):
        self.client.force_authenticate(_admin())
        with mock.patch.dict(os.environ, {"NESSUS_URL": "https://n"}):
            body = self.client.get(URL).json()
        field = SecretTests._field(body, "NESSUS_URL")
        self.assertTrue(field["env_locked"])
        self.assertEqual(field["source"], "env")

    def test_test_endpoint_rejects_unknown_target(self):
        self.client.force_authenticate(_admin())
        resp = self.client.post(URL + "test/", {"target": "ldap"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_email_test_needs_a_host(self):
        self.client.force_authenticate(_admin())
        resp = self.client.post(URL + "test/", {"target": "email"}, format="json")
        self.assertFalse(resp.json()["ok"])


class AuditTests(TestCase):
    def setUp(self):
        from apps_business.audits.apps import wire

        wire()  # other tests hooks.clear(); re-wiring is idempotent
        config.invalidate()
        self.client = APIClient()
        self.client.force_authenticate(_admin())

    def tearDown(self):
        config.invalidate()

    def test_change_is_audited_by_name_only(self):
        from apps_business.audits.models import AuditEvent

        self.client.post(URL, {"values": {
            "GREENBONE_PASSWORD": "hunter2",
            "GREENBONE_URL": "gvm.lan:9390",
        }}, format="json")
        event = AuditEvent.objects.filter(action="settings.changed").first()
        self.assertIsNotNone(event)
        self.assertIn("GREENBONE_PASSWORD", event.target)
        self.assertNotIn("hunter2", str(event.detail))

    def test_no_change_no_audit(self):
        from apps_business.audits.models import AuditEvent

        self.client.post(URL, {"values": {}}, format="json")
        self.assertFalse(AuditEvent.objects.filter(action="settings.changed").exists())


class ConsumerTests(TestCase):
    """The settings have to reach the code that uses them, not just the table."""

    def setUp(self):
        config.invalidate()

    def tearDown(self):
        config.invalidate()

    @override_settings(GREENBONE_URL="", GREENBONE_USERNAME="", GREENBONE_PASSWORD="")
    def test_greenbone_reads_stored_credentials(self):
        from apps.vulns.scanners.greenbone import GreenboneScanner

        scanner = GreenboneScanner()
        self.assertFalse(scanner.configured())
        config.write("GREENBONE_URL", "gvm.lan:9390")
        config.write("GREENBONE_USERNAME", "admin")
        config.write("GREENBONE_PASSWORD", "hunter2")
        self.assertTrue(scanner.configured())

    @override_settings(NESSUS_URL="", NESSUS_ACCESS_KEY="", NESSUS_SECRET_KEY="")
    def test_nessus_reads_stored_credentials(self):
        from apps.vulns.scanners.nessus import NessusScanner

        scanner = NessusScanner()
        self.assertIsNone(scanner._session())
        config.write("NESSUS_URL", "https://nessus.lan:8834/")
        config.write("NESSUS_ACCESS_KEY", "a")
        config.write("NESSUS_SECRET_KEY", "s")
        base_url, headers, _verify = scanner._session()
        self.assertEqual(base_url, "https://nessus.lan:8834")
        self.assertIn("accessKey=a", headers["X-ApiKeys"])

    def test_timezone_reaches_the_ui_context(self):
        config.write("VIGIL_TIMEZONE", "America/New_York")
        client = APIClient()
        client.force_authenticate(_admin("tzadmin"))
        body = client.get("/api/v1/about/").json()
        self.assertEqual(body["timezone"], "America/New_York")

    def test_stored_smtp_host_switches_off_the_console_backend(self):
        """Vigil prints mail by default. Once someone enters an SMTP host they
        have said what they want, and "I configured mail and nothing arrived"
        must not be the outcome."""
        with override_settings(
                EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"):
            self.assertIn("console", type(config.mail_connection()).__module__)
            config.write("EMAIL_HOST", "smtp.example.com")
            connection = config.mail_connection()
            self.assertIn("smtp", type(connection).__module__)
            self.assertEqual(connection.host, "smtp.example.com")

    def test_explicit_backend_in_the_environment_still_wins(self):
        config.write("EMAIL_HOST", "smtp.example.com")
        with mock.patch.dict(
                os.environ,
                {"EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}), \
                override_settings(
                    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertIn("locmem", type(config.mail_connection()).__module__)
