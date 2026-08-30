"""Characterization tests for tag matching, written BEFORE the move to
database-defined tags (docs/internal/SPEC_database_tags.md).

These pin what tag matching does *today*, against the string implementation.
They are not aspirational: several of them assert behaviour that is arguably
wrong, and say so. The point is that the migration must either preserve each
behaviour or change it deliberately, with the test updated in the same commit
so the change is visible in review rather than discovered in production.

The headline finding is that the four matchers do not agree with each other:

    matcher                whitespace   empty tag list   rejected hosts
    Baseline.matches       not stripped  matches ALL      not excluded
    wave_host_ids          not stripped  matches NONE     excluded
    definition_deploy      STRIPPED      n/a              n/a
    automations            not stripped  n/a              n/a

Empty-list handling is *opposite* between baselines and waves. Whitespace is
stripped in exactly one of the four. Any of these is a plausible surprise for
an operator, and unifying them is the main behavioural win of the migration.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations.models import Automation
from apps.baselines.models import Baseline
from apps.hosts.models import Host
from apps.tasks.models import PatchWave, wave_host_ids


def _host(name, tags, status=Host.Status.ONLINE, ip=None):
    return Host.objects.create(
        hostname=name, ip_address=ip or f"10.20.0.{abs(hash(name)) % 200 + 1}",
        tags=tags, status=status, agent_token=f"tok-{name}")


class TagNormalisationTests(TestCase):
    """How a tag string is compared. Case is folded everywhere; whitespace is
    not, except in one place."""

    def test_matching_is_case_insensitive_everywhere(self):
        host = _host("h-case", ["PROD"])
        self.assertTrue(Baseline(enabled=True, target_tags=["prod"]).matches(host))
        wave = PatchWave.objects.create(name="w-case", order=901, tags=["prod"])
        self.assertIn(host.id, wave_host_ids(wave))

    def test_whitespace_is_NOT_stripped_by_baselines(self):
        """A trailing space makes the tag a different tag. Arguably a bug; it
        is what happens today, and an operator typing "prod " into a baseline
        gets silence rather than an error."""
        host = _host("h-ws", ["prod"])
        self.assertFalse(Baseline(enabled=True, target_tags=["prod "]).matches(host))

    def test_whitespace_is_NOT_stripped_by_waves_either(self):
        host = _host("h-ws2", ["prod"])
        wave = PatchWave.objects.create(name="w-ws", order=902, tags=["prod "])
        self.assertNotIn(host.id, wave_host_ids(wave))

    def test_a_wholly_blank_wave_tag_is_dropped_not_matched(self):
        """wave_host_ids filters blank entries, so ["", "prod"] behaves as
        ["prod"] rather than matching a host with an empty-string tag."""
        host = _host("h-blank", ["prod"])
        wave = PatchWave.objects.create(name="w-blank", order=903, tags=["", "  ", "prod"])
        self.assertIn(host.id, wave_host_ids(wave))

    def test_unicode_tags_compare_by_simple_lowercasing(self):
        host = _host("h-uni", ["PRODUÇÃO"])
        self.assertTrue(Baseline(enabled=True, target_tags=["produção"]).matches(host))


class EmptyTagListTests(TestCase):
    """The single most surprising disagreement between the matchers."""

    def test_baseline_with_no_target_tags_matches_EVERY_host(self):
        host = _host("h-any", [])
        self.assertTrue(Baseline(enabled=True, target_tags=[]).matches(host))

    def test_wave_with_no_tags_matches_NO_hosts(self):
        """Opposite of the baseline rule above. Same empty list, inverse
        meaning, depending on which feature you are using."""
        _host("h-none", ["anything"])
        wave = PatchWave.objects.create(name="w-empty", order=904, tags=[])
        self.assertEqual(wave_host_ids(wave), [])


class MembershipTests(TestCase):
    def test_any_tag_overlap_is_enough(self):
        """Membership is OR, not AND: one tag in common puts the host in."""
        host = _host("h-or", ["linux", "web"])
        self.assertTrue(Baseline(enabled=True, target_tags=["web", "db"]).matches(host))

    def test_a_host_with_no_tags_matches_no_tagged_selector(self):
        host = _host("h-untagged", [])
        self.assertFalse(Baseline(enabled=True, target_tags=["web"]).matches(host))
        wave = PatchWave.objects.create(name="w-or", order=905, tags=["web"])
        self.assertNotIn(host.id, wave_host_ids(wave))

    def test_waves_exclude_rejected_hosts(self):
        rejected = _host("h-rejected", ["web"], status=Host.Status.REJECTED)
        wave = PatchWave.objects.create(name="w-rej", order=906, tags=["web"])
        self.assertNotIn(rejected.id, wave_host_ids(wave))

    def test_baseline_matching_does_NOT_exclude_rejected_hosts(self):
        """Baseline.matches has no status check — the caller is expected to
        have filtered already. Different from waves, and worth knowing."""
        rejected = _host("h-rejected2", ["web"], status=Host.Status.REJECTED)
        self.assertTrue(Baseline(enabled=True, target_tags=["web"]).matches(rejected))

    def test_disabled_baseline_matches_nothing(self):
        host = _host("h-disabled", ["web"])
        self.assertFalse(Baseline(enabled=False, target_tags=["web"]).matches(host))


class WaveExclusivityTests(TestCase):
    """A host in several waves belongs to the earliest one only. This is the
    behaviour the whole staged-rollout safety story rests on."""

    def test_earliest_wave_claims_a_shared_host(self):
        from apps.tasks.models import rollout_wave_plan

        host = _host("h-shared", ["canary", "broad"])
        w1 = PatchWave.objects.create(name="c", order=911, tags=["canary"])
        w2 = PatchWave.objects.create(name="b", order=912, tags=["broad"])
        plan = rollout_wave_plan([w1, w2])
        self.assertIn(host.id, plan[w1.id])
        self.assertNotIn(host.id, plan[w2.id])

    def test_plan_order_follows_wave_order_not_list_order(self):
        from apps.tasks.models import rollout_wave_plan

        host = _host("h-shared2", ["a", "b"])
        w1 = PatchWave.objects.create(name="first", order=913, tags=["a"])
        w2 = PatchWave.objects.create(name="second", order=914, tags=["b"])
        # Deliberately pass them out of order.
        plan = rollout_wave_plan([w2, w1])
        self.assertIn(host.id, plan[w1.id])
        self.assertNotIn(host.id, plan[w2.id])


class AutomationTagTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tagauto", password="x")

    def test_event_tags_scope_which_events_fire(self):
        from apps.automations.engine import tags_ok

        host = _host("h-evt", ["prod"])
        self.assertTrue(tags_ok(Automation(event_tags=["prod"]), host))
        self.assertFalse(tags_ok(Automation(event_tags=["staging"]), host))

    def test_no_event_tags_means_any_host(self):
        from apps.automations.engine import tags_ok

        host = _host("h-evt2", ["whatever"])
        self.assertTrue(tags_ok(Automation(event_tags=[]), host))

    def test_event_tags_with_no_host_do_not_match(self):
        """A tag-scoped automation needs a host to check against; an event
        without one is not a match rather than a wildcard."""
        from apps.automations.engine import tags_ok

        self.assertFalse(tags_ok(Automation(event_tags=["prod"]), None))


class CollisionInventoryTests(TestCase):
    """What the migration will have to merge.

    These do not assert a behaviour so much as record the shape of the problem:
    three spellings that are one tag under today's comparison, and which must
    collapse to a single Tag row without changing anyone's membership.
    """

    def test_case_variants_are_already_one_tag_in_practice(self):
        a = _host("h-c1", ["Prod"])
        b = _host("h-c2", ["prod"])
        c = _host("h-c3", ["PROD"])
        wave = PatchWave.objects.create(name="w-coll", order=921, tags=["prod"])
        ids = set(wave_host_ids(wave))
        self.assertEqual(ids, {a.id, b.id, c.id},
                         "case variants already match as one tag; the migration "
                         "must merge them into one row without changing this")

    def test_whitespace_variants_are_NOT_one_tag_today(self):
        """`prod ` is a separate tag right now. The migration will normalise it
        into `prod`, which CHANGES membership — deliberately, and this test is
        the record of what changes."""
        spaced = _host("h-c4", ["prod "])
        wave = PatchWave.objects.create(name="w-coll2", order=922, tags=["prod"])
        self.assertNotIn(spaced.id, wave_host_ids(wave))


class ReprovisionTagTests(TestCase):
    """Rebuild tagging — the two sources the first draft of the spec missed.

    A profile carries standing `completion_tags`; a job carries one extra
    `completion_tag` as a plain string. Both land on the host when the rebuild
    finishes, so both must migrate or a rebuild silently stops tagging.
    """

    def _apply(self, host, profile_tags, job_tag=""):
        from apps.reprovision.completion import _apply_completion_tags

        class _Profile:
            completion_tags = profile_tags

        class _Job:
            profile = _Profile()
            completion_tag = job_tag

        _apply_completion_tags(_Job(), host)
        host.refresh_from_db()
        return host.tags

    def test_profile_tags_land_on_the_host(self):
        host = _host("h-rp1", [])
        self.assertEqual(self._apply(host, ["rebuilt", "linux"]), ["rebuilt", "linux"])

    def test_job_one_off_tag_lands_too(self):
        host = _host("h-rp2", [])
        self.assertIn("ticket-4412", self._apply(host, [], "ticket-4412"))

    def test_completion_tags_ARE_stripped(self):
        """Unlike baselines and waves, this path strips. Fourth distinct
        normalisation rule in the codebase."""
        host = _host("h-rp3", [])
        self.assertIn("rebuilt", self._apply(host, ["  rebuilt  "]))

    def test_blank_tags_are_skipped(self):
        host = _host("h-rp4", ["keep"])
        self.assertEqual(self._apply(host, ["", "   "], ""), ["keep"])

    def test_agent_namespace_is_refused(self):
        """`agent:*` belongs to what the agent advertises about itself. An
        operator-set tag must not be able to impersonate one."""
        host = _host("h-rp5", [])
        self.assertEqual(self._apply(host, ["agent:spoofed"], "agent:also-spoofed"), [])

    def test_existing_tags_are_preserved_and_not_duplicated(self):
        host = _host("h-rp6", ["existing"])
        result = self._apply(host, ["existing", "new"])
        self.assertEqual(result.count("existing"), 1)
        self.assertIn("new", result)
