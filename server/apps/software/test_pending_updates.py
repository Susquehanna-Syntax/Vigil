"""Pending updates: the Windows list from the check-in, Linux from inventory,
and first_seen surviving every report so an update's age is real."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.hosts.models import Host

from .models import PendingUpdate, SoftwareItem
from .test_software_ingest import digest_of, ingest_software, item, payload


def _update(kb, severity="critical", categories=("Security Updates",), **extra):
    return {"update_id": f"id-{kb}", "kb": kb, "title": f"Update {kb}",
            "severity": severity, "categories": list(categories),
            "reboot_required": False, **extra}


class WindowsListTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="win", agent_token="wtok",
                                        status=Host.Status.ONLINE, mode="managed")

    def _checkin(self, body):
        return self.client.post("/api/v1/checkin", {"hostname": "win", **body},
                                content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer wtok")

    def test_list_is_stored_and_replaced_keeping_first_seen(self):
        self._checkin({"windows_update_list": [
            _update("KB1"), _update("", categories=["Windows Defender", "Definition Updates"])]})
        rows = {p.key: p for p in PendingUpdate.objects.filter(host=self.host)}
        self.assertEqual(set(rows), {"KB1", "id-"})
        self.assertEqual(rows["KB1"].classification, "Security Updates")
        self.assertEqual(rows["id-"].classification, "Definition Updates")
        old = timezone.now() - timedelta(days=20)
        PendingUpdate.objects.filter(key="KB1").update(first_seen=old)

        self._checkin({"windows_update_list": [_update("KB1"), _update("KB2")]})
        rows = {p.key: p for p in PendingUpdate.objects.filter(host=self.host)}
        self.assertEqual(set(rows), {"KB1", "KB2"}, "an installed update drops out")
        self.assertEqual(rows["KB1"].first_seen, old)
        self.assertEqual(rows["KB1"].kind, "windows")

    def test_absent_key_leaves_rows_alone_and_empty_list_clears(self):
        self._checkin({"windows_update_list": [_update("KB1")]})
        self._checkin({"windows_updates": {"pending": 1}})
        self.assertEqual(PendingUpdate.objects.count(), 1)
        self._checkin({"windows_update_list": []})
        self.assertEqual(PendingUpdate.objects.count(), 0)

    def test_junk_is_ignored(self):
        self._checkin({"windows_update_list": "nope"})
        self._checkin({"windows_update_list": [None, {"title": "no key"}, 7]})
        self.assertEqual(PendingUpdate.objects.count(), 0)


class LinuxPendingTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="lin", agent_token="ltok",
                                        status=Host.Status.ONLINE)

    def _report(self, *items):
        ingest_software(self.host, payload(digest_of(*[i["id"] + i["version"] + i["latest"]
                                                         for i in items]), list(items)))

    def test_outdated_since_and_linux_rows(self):
        self._report(item("dpkg", "curl", "curl", "8.4", "8.5"),
                     item("dpkg", "vim", "vim", "9.0", "9.0"),
                     item("snap", "lxd", "lxd", "5.0", "5.1"))
        curl = SoftwareItem.objects.get(package_id="curl")
        self.assertIsNotNone(curl.outdated_since)
        self.assertIsNone(SoftwareItem.objects.get(package_id="vim").outdated_since)
        pending = PendingUpdate.objects.get(host=self.host)
        self.assertEqual((pending.kind, pending.key, pending.version),
                         ("linux", "curl", "8.5"))
        self.assertEqual(pending.first_seen, curl.outdated_since,
                         "a snap is app policy territory, not patching")

        # Still outdated on the next report: the clock keeps running.
        since = curl.outdated_since
        later = timezone.now() + timedelta(hours=1)
        with mock.patch("apps.software.ingest.timezone.now", return_value=later):
            self._report(item("dpkg", "curl", "curl", "8.4", "8.6"))
        self.assertEqual(SoftwareItem.objects.get(package_id="curl").outdated_since, since)

        # Upgraded: cleared, and the pending row goes.
        self._report(item("dpkg", "curl", "curl", "8.6", "8.6"))
        self.assertIsNone(SoftwareItem.objects.get(package_id="curl").outdated_since)
        self.assertFalse(PendingUpdate.objects.exists())
