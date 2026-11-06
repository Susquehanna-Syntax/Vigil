"""The backup settings group and the backup history endpoint.

The settings are ordinary instance settings; what is specific to backups is
who may see the history. A backup holds every site's database, every upload
and the generated secrets, so a site admin reads the group but never the runs.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.instance import config
from apps.instance.models import BackupRun, InstanceSetting
from vigil.testsupport import requires_business

URL = "/api/v1/instance-settings/"
RUNS_URL = "/api/v1/instance-settings/backups/"

# A source checkout runs without the Business package; only the site-admin
# half of the permission test needs it.
try:
    from apps_business.sites.models import Site, UserSiteRole
except ImportError:
    Site = UserSiteRole = None


def _admin(username="backup-admin"):
    user = get_user_model().objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=Role.ADMIN)
    return user


def _viewer(username="backup-viewer"):
    user = get_user_model().objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=Role.VIEWER)
    return user


def _run(**kwargs):
    started = kwargs.pop("started_at", timezone.now())
    kwargs.setdefault("started_at", started)
    kwargs.setdefault("state", BackupRun.State.OK)
    kwargs.setdefault("finished_at", started + timedelta(seconds=30))
    return BackupRun.objects.create(**kwargs)


class BackupSettingsTests(TestCase):
    def setUp(self):
        config.invalidate()
        self.client = APIClient()

    def tearDown(self):
        config.invalidate()

    def test_the_backups_group_is_offered_to_an_instance_admin(self):
        self.client.force_authenticate(_admin())
        groups = self.client.get(URL).json()["groups"]
        backups = next(g for g in groups if g["id"] == "backups")
        self.assertEqual(
            sorted(f["name"] for f in backups["fields"]),
            ["VIGIL_BACKUP_ENABLED", "VIGIL_BACKUP_HOUR", "VIGIL_BACKUP_KEEP",
             "VIGIL_BACKUP_PASSPHRASE", "VIGIL_BACKUP_S3_ACCESS_KEY", "VIGIL_BACKUP_S3_BUCKET",
             "VIGIL_BACKUP_S3_REGION", "VIGIL_BACKUP_S3_SECRET_KEY", "VIGIL_BACKUP_S3_URL"])
        secrets = {f["name"]: f for f in backups["fields"]
                   if f["name"] in ("VIGIL_BACKUP_PASSPHRASE", "VIGIL_BACKUP_S3_SECRET_KEY")}
        self.assertTrue(all(f["kind"] == "secret" for f in secrets.values()), secrets)

    def test_saving_the_hour(self):
        self.client.force_authenticate(_admin())
        resp = self.client.post(URL, {"values": {"VIGIL_BACKUP_HOUR": 3}}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(config.setting("VIGIL_BACKUP_HOUR"), 3)

    def test_an_hour_outside_the_day_is_rejected(self):
        self.client.force_authenticate(_admin())
        for hour in ("24", "-1"):
            resp = self.client.post(URL, {"values": {"VIGIL_BACKUP_HOUR": hour}}, format="json")
            self.assertEqual(resp.status_code, 400, hour)
        self.assertFalse(InstanceSetting.objects.filter(key="VIGIL_BACKUP_HOUR").exists())


class BackupRunListTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_runs_are_listed_newest_first(self):
        now = timezone.now()
        old = _run(started_at=now - timedelta(hours=26), name="vigil-2026-11-04.tar.gz",
                   size_bytes=1048576, finished_at=now - timedelta(hours=25))
        new = _run(started_at=now, name="vigil-2026-11-05.tar.gz", size_bytes=2097152,
                   state=BackupRun.State.FAILED, error="pg_dump: connection to server failed")
        self.client.force_authenticate(_admin())
        resp = self.client.get(RUNS_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 2)
        self.assertEqual([r["id"] for r in body], [new.id, old.id])
        self.assertEqual(body[0]["state"], "failed")
        self.assertEqual(body[0]["size_bytes"], 2097152)
        self.assertEqual(body[0]["name"], "vigil-2026-11-05.tar.gz")
        self.assertEqual(body[0]["finished_at"], new.finished_at.isoformat())
        self.assertEqual(body[1]["finished_at"], old.finished_at.isoformat())

    def test_a_run_that_has_not_finished_has_no_finish_time(self):
        _run(state=BackupRun.State.RUNNING, finished_at=None)
        self.client.force_authenticate(_admin())
        self.assertIsNone(self.client.get(RUNS_URL).json()[0]["finished_at"])

    def test_the_list_stops_at_twenty_runs(self):
        """The card is a summary, not a pager: 21 rows must not become 21
        lines on a settings page."""
        now = timezone.now()
        for i in range(21):
            _run(started_at=now - timedelta(hours=i))
        self.client.force_authenticate(_admin())
        self.assertEqual(len(self.client.get(RUNS_URL).json()), 20)

    def test_an_error_is_capped(self):
        _run(state=BackupRun.State.FAILED, error="x" * 5000)
        self.client.force_authenticate(_admin())
        self.assertEqual(len(self.client.get(RUNS_URL).json()[0]["error"]), 2000)

    def test_runs_are_instance_admin_only(self):
        _run()
        self.client.force_authenticate(_viewer())
        self.assertEqual(self.client.get(RUNS_URL).status_code, 403)
        # A site admin is an admin somewhere and nothing install-wide here.
        if Site is None:
            self.skipTest("needs the vigil-business package")
        dana = _viewer("backup-site-admin")
        UserSiteRole.objects.create(user=dana, site=Site.objects.create(name="West", slug="west"),
                                    role=Role.ADMIN)
        self.client.force_authenticate(dana)
        self.assertEqual(self.client.get(RUNS_URL).status_code, 403)

    @requires_business
    def test_the_backups_group_is_greyed_for_a_site_admin(self):
        """A site admin may set the schedule and must never read the runs:
        one archive holds every site's data and the install's secrets."""
        dana = _viewer("backup-greyed")
        UserSiteRole.objects.create(user=dana, site=Site.objects.create(name="East", slug="east"),
                                    role=Role.ADMIN)
        self.client.force_authenticate(dana)
        backups = next(g for g in self.client.get(URL).json()["groups"] if g["id"] == "backups")
        self.assertTrue(backups["read_only"])
        self.assertEqual(self.client.get(RUNS_URL).status_code, 403)
