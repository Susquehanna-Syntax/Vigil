""""Outdated" means behind, not merely different.

The check used to be ``host.agent_version != current_version``. That reads
as "outdated" in one direction only if you assume the server is always the
newest thing in the fleet — and the moment it isn't, every host in the fleet
gets a warning telling the operator to upgrade an agent that is already
ahead of the server. Observed in production: rolling the server back to
2026.6.2 lit up hosts running 2026.7.0 as "outdated ... expected 2026.6.2".

Nothing about that is actionable. The server is the thing that is behind.
"""
from django.test import TestCase, override_settings

from apps.hosts.models import Host

from .models import Alert
from .tasks import _is_older, _version_key, check_outdated_agents


class VersionKeyTests(TestCase):
    def test_a_plain_calver_string_parses(self):
        self.assertEqual(_version_key("2026.7.1"), (2026, 7, 1))

    def test_a_leading_v_is_ignored(self):
        self.assertEqual(_version_key("v2026.7.1"), (2026, 7, 1))

    def test_a_trailing_letter_is_tolerated(self):
        """Vigil shipped 2026.1.8b; it must not read as unparseable."""
        self.assertEqual(_version_key("2026.1.8b"), (2026, 1, 8))

    def test_unparseable_input_is_none_not_a_guess(self):
        for junk in ("", "unknown", "dev", "..", "2026..1"):
            self.assertIsNone(_version_key(junk), junk)


class IsOlderTests(TestCase):
    def test_a_behind_agent_is_older(self):
        self.assertTrue(_is_older("2026.6.2", "2026.7.1"))

    def test_an_equal_agent_is_not_older(self):
        self.assertFalse(_is_older("2026.7.1", "2026.7.1"))

    def test_an_ahead_agent_is_not_older(self):
        """The production regression, stated directly."""
        self.assertFalse(_is_older("2026.7.0", "2026.6.2"))

    def test_comparison_is_numeric_not_lexicographic(self):
        """String order puts "2026.10.0" before "2026.9.0"; numbers do not."""
        self.assertFalse(_is_older("2026.10.0", "2026.9.0"))
        self.assertTrue(_is_older("2026.9.0", "2026.10.0"))

    def test_differing_lengths_compare_on_equal_footing(self):
        self.assertFalse(_is_older("2026.7", "2026.7.0"))
        self.assertFalse(_is_older("2026.7.0", "2026.7"))
        self.assertTrue(_is_older("2026.7", "2026.7.1"))

    def test_an_unreadable_version_never_warns(self):
        self.assertFalse(_is_older("garbage", "2026.7.1"))
        self.assertFalse(_is_older("2026.7.1", "garbage"))


@override_settings(VIGIL_AGENT_VERSION="2026.6.2")
class CheckOutdatedAgentsTests(TestCase):
    """End to end, at the version pairing that caused the report."""

    def _host(self, name, version):
        return Host.objects.create(
            hostname=name, agent_token=name.ljust(32, "t"),
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
            agent_version=version,
        )

    def _outdated_alerts(self, host):
        return Alert.objects.filter(
            host=host, message__startswith="Agent outdated:",
            state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED])

    def test_an_agent_ahead_of_the_server_raises_nothing(self):
        host = self._host("ahead", "2026.7.0")
        check_outdated_agents()
        self.assertFalse(self._outdated_alerts(host).exists())

    def test_an_agent_behind_the_server_still_fires(self):
        host = self._host("behind", "2026.5.0")
        check_outdated_agents()
        self.assertTrue(self._outdated_alerts(host).exists())

    def test_a_matching_agent_raises_nothing(self):
        host = self._host("current", "2026.6.2")
        check_outdated_agents()
        self.assertFalse(self._outdated_alerts(host).exists())

    def test_a_stale_alert_is_resolved_once_the_server_rolls_back(self):
        """The cleanup the operator asked for: alerts raised while the
        server was ahead must clear themselves, not need hand-dismissing."""
        host = self._host("rolled", "2026.7.0")
        stale = Alert.objects.create(
            host=host, rule=None, state=Alert.State.FIRING, severity="warning",
            message=f"Agent outdated: {host.hostname} is running v2026.7.0 "
                    f"(current: v2026.7.1)",
        )
        check_outdated_agents()
        stale.refresh_from_db()
        self.assertEqual(stale.state, Alert.State.RESOLVED)
        self.assertIsNotNone(stale.resolved_at)

    def test_an_acknowledged_stale_alert_is_resolved_too(self):
        host = self._host("acked", "2026.7.0")
        stale = Alert.objects.create(
            host=host, rule=None, state=Alert.State.ACKNOWLEDGED,
            severity="warning",
            message=f"Agent outdated: {host.hostname} is running v2026.7.0",
        )
        check_outdated_agents()
        stale.refresh_from_db()
        self.assertEqual(stale.state, Alert.State.RESOLVED)

    def test_a_host_with_no_reported_version_is_skipped(self):
        host = self._host("silent", "")
        check_outdated_agents()
        self.assertFalse(self._outdated_alerts(host).exists())
