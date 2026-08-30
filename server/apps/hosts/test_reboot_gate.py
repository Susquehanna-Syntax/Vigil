"""The reboot version gate and reboot_required ingest (phase 08b).

An agent older than 2026.9.0 reads reboot params with ``params.get(...)``
and drops anything it does not recognise — a ``defer_limit`` sent to a
2026.8.0 agent becomes an unannounced ``shutdown -r now``. The server
therefore refuses to dispatch deferral-bearing reboots to old agents and
fails the task with a message naming the required version.
"""
import secrets

from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.hosts.versions import version_at_least
from apps.tasks.models import Task

_CHECKIN = "/api/v1/checkin"


def _make_task(host, *, action="reboot", params=None, **kw):
    return Task.objects.create(
        host=host,
        action=action,
        params=params or {},
        state=Task.State.PENDING,
        nonce=secrets.token_hex(32),
        **kw,
    )


def _checkin(client, host, body):
    return client.post(
        _CHECKIN,
        body,
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {host.agent_token}",
    )


class VersionCompareTests(TestCase):
    """version_at_least is a numeric, segment-wise comparison."""

    def test_version_compare_is_numeric(self):
        # The case that catches a lexical comparison.
        self.assertTrue(version_at_least("2026.10.0", "2026.9.0"))
        self.assertTrue(version_at_least("2026.9.0", "2026.9.0"))
        self.assertFalse(version_at_least("2026.8.0", "2026.9.0"))
        self.assertTrue(version_at_least("2027.0.1", "2026.10.0"))

    def test_blank_version_is_not_at_least(self):
        self.assertFalse(version_at_least("", "2026.9.0"))

    def test_unparseable_version_is_not_at_least(self):
        self.assertFalse(version_at_least("garbage", "2026.9.0"))
        self.assertFalse(version_at_least("2026.x", "2026.9.0"))


class RebootGateTests(TestCase):
    """Deferral-bearing reboots are refused for agents below 2026.9.0."""

    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-01",
            agent_token="tok-" + "e" * 32,
            status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED,
        )

    def _refused_task(self, params):
        task = _make_task(self.host, params=params)
        _checkin(self.client, self.host, {"hostname": self.host.hostname})
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.assertIn("2026.9.0", task.result_output)
        return task

    def _dispatched_task_ids(self, params):
        task = _make_task(self.host, params=params)
        resp = _checkin(self.client, self.host, {"hostname": self.host.hostname})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["id"] for t in resp.json()["tasks"]], [str(task.id)])
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.DISPATCHED)
        return task

    def _set_agent_version(self, version):
        self.host.agent_version = version
        self.host.save()

    def test_old_agent_refused_for_deferral_params(self):
        self._set_agent_version("2026.8.0")
        for param, value in (
            ("notify", True),
            ("defer_limit", 3),
            ("defer_minutes", 30),
        ):
            with self.subTest(param=param):
                self._refused_task({param: value})

    def test_current_agent_accepted(self):
        self._set_agent_version("2026.9.0")
        self._dispatched_task_ids({"defer_limit": 3})

    def test_blank_agent_version_refused(self):
        self._set_agent_version("")
        self._refused_task({"defer_limit": 3})

    def test_unparseable_agent_version_refused(self):
        for version in ("garbage", "2026.x", ""):
            with self.subTest(version=version):
                self._set_agent_version(version)
                self._refused_task({"defer_limit": 3})

    def test_plain_reboot_still_dispatches_to_old_agent(self):
        self._set_agent_version("2026.8.0")
        self._dispatched_task_ids({"delay_seconds": 300})
        self._dispatched_task_ids({})

    def test_windowed_reboot_behaves_like_any_windowed_task(self):
        """A deferral-bearing reboot with a schedule.window is held until
        the window opens, exactly like any other windowed task (the gate
        only fires on dispatch)."""
        from zoneinfo import ZoneInfo

        from django.conf import settings as django_settings
        from django.utils.timezone import now

        self._set_agent_version("2026.9.0")
        local = now().astimezone(ZoneInfo(django_settings.VIGIL_TIMEZONE))
        closed_day = (local.weekday() + 1) % 7
        task = _make_task(
            self.host,
            params={"defer_limit": 3},
            schedule={"window": {"days": [closed_day], "start_hour": 0, "end_hour": 23}},
        )
        resp = _checkin(self.client, self.host, {"hostname": self.host.hostname})
        self.assertEqual(resp.json()["tasks"], [])
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.PENDING)


class RebootRequiredIngestTests(TestCase):
    """The check-in payload's reboot_required is stored; an absent key is
    not the same as False."""

    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-02",
            agent_token="tok-" + "f" * 32,
            status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED,
        )

    def test_missing_reboot_required_preserves_value(self):
        self.host.reboot_required = True
        self.host.save()
        _checkin(self.client, self.host, {"hostname": self.host.hostname})
        self.host.refresh_from_db()
        self.assertTrue(self.host.reboot_required)

    def test_false_reboot_required_is_stored(self):
        self.host.reboot_required = True
        self.host.save()
        _checkin(self.client, self.host, {"reboot_required": False})
        self.host.refresh_from_db()
        self.assertFalse(self.host.reboot_required)

    def test_true_reboot_required_is_stored(self):
        _checkin(self.client, self.host, {"reboot_required": True})
        self.host.refresh_from_db()
        self.assertTrue(self.host.reboot_required)

    def test_reboot_required_exposed_on_serializer(self):
        from apps.hosts.serializers import HostSerializer

        self.host.reboot_required = True
        self.host.save()
        data = HostSerializer(self.host).data
        self.assertIn("reboot_required", data)
        self.assertTrue(data["reboot_required"])
