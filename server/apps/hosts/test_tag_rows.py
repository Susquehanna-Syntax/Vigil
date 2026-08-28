"""The Tag row model and the population that seeds it from existing strings.

The rule under test throughout: **if two spellings did not match before, they
must not start matching now.** Case variants collapse to one row because they
already matched; whitespace variants stay separate because they never did.
"""

from django.test import TestCase

from apps.automations.models import Automation
from apps.baselines.models import Baseline
from apps.hosts.models import Host, Tag
from apps.tasks.models import PatchWave


class TagCanonicalisationTests(TestCase):
    def test_case_variants_are_one_row(self):
        first, created_a = Tag.get_or_create_by_name("Prod")
        second, created_b = Tag.get_or_create_by_name("prod")
        third, created_c = Tag.get_or_create_by_name("PROD")
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertFalse(created_c)
        self.assertEqual(first.pk, second.pk, "case already matched; must stay one tag")
        self.assertEqual(Tag.objects.count(), 1)

    def test_whitespace_variants_are_separate_rows(self):
        """`prod ` never matched `prod`, so merging them would move hosts
        between waves. It stays its own tag."""
        plain, _ = Tag.get_or_create_by_name("prod")
        spaced, created = Tag.get_or_create_by_name("prod ")
        self.assertTrue(created)
        self.assertNotEqual(plain.pk, spaced.pk)
        self.assertEqual(Tag.objects.count(), 2)

    def test_key_is_lowercased_but_not_stripped(self):
        tag, _ = Tag.get_or_create_by_name("  Prod  ")
        self.assertEqual(tag.key, "  prod  ")
        self.assertEqual(tag.name, "  Prod  ")

    def test_blank_name_is_refused(self):
        for blank in ("", "   ", "\t"):
            with self.subTest(blank=blank):
                with self.assertRaises(ValueError):
                    Tag.get_or_create_by_name(blank)

    def test_unicode_case_folding(self):
        a, _ = Tag.get_or_create_by_name("PRODUÇÃO")
        b, created = Tag.get_or_create_by_name("produção")
        self.assertFalse(created)
        self.assertEqual(a.pk, b.pk)


class TagKindTests(TestCase):
    def test_auto_prefixes_are_classified(self):
        for name in ("os:debian", "os_family:linux", "pkg:apt", "arch:amd64"):
            with self.subTest(name=name):
                self.assertEqual(Tag.kind_for(name), Tag.Kind.AUTO)

    def test_agent_prefix_is_classified(self):
        self.assertEqual(Tag.kind_for("agent:role-web"), Tag.Kind.AGENT)

    def test_ordinary_tags_are_manual(self):
        self.assertEqual(Tag.kind_for("prod"), Tag.Kind.MANUAL)

    def test_reserved_detection_is_case_insensitive(self):
        """A reserved namespace must not be bypassable by capitalising it —
        `agent:*` exists so a compromised agent cannot impersonate an
        operator-set tag."""
        for name in ("AGENT:spoof", "Agent:spoof", "OS:debian"):
            with self.subTest(name=name):
                self.assertTrue(Tag.is_reserved(name))

    def test_ordinary_tag_is_not_reserved(self):
        self.assertFalse(Tag.is_reserved("production"))

    def test_kind_is_set_on_save(self):
        tag, _ = Tag.get_or_create_by_name("os:ubuntu")
        self.assertEqual(tag.kind, Tag.Kind.AUTO)


class PopulationTests(TestCase):
    """The migration's collector, run against a deliberately messy fleet."""

    def _collect(self):
        """Call the migration's own collector, so this tests what actually
        runs rather than a copy of it."""
        import importlib

        from django.apps import apps as django_apps

        mod = importlib.import_module("apps.hosts.migrations.0012_populate_tags")
        return mod.collect_tag_names(django_apps)

    def setUp(self):
        Host.objects.create(hostname="h1", ip_address="10.30.0.1",
                            agent_token="t1", tags=["Prod", "web"])
        Host.objects.create(hostname="h2", ip_address="10.30.0.2",
                            agent_token="t2", tags=["prod", "os:debian"])
        Host.objects.create(hostname="h3", ip_address="10.30.0.3",
                            agent_token="t3", tags=["prod "])   # trailing space
        PatchWave.objects.create(name="w", order=801, tags=["canary"])
        Baseline.objects.create(name="b", target_tags=["web"])
        Automation.objects.create(name="a", trigger=Automation.Trigger.EVENT,
                                  event="alert_fired",
                                  action_kind=Automation.ActionKind.TASK,
                                  event_tags=["prod"], target_tags=["db"])

    def test_collector_finds_every_source(self):
        seen = self._collect()
        for expected in ("prod", "web", "canary", "db", "os:debian"):
            self.assertIn(expected, seen, f"{expected!r} missing from collection")

    def test_case_variants_collapse_to_one_key(self):
        seen = self._collect()
        self.assertEqual(seen["prod"]["names"], {"Prod", "prod"})

    def test_whitespace_variant_is_its_own_key(self):
        seen = self._collect()
        self.assertIn("prod ", seen)
        self.assertNotEqual(seen["prod"]["names"], seen["prod "]["names"])

    def test_blank_tags_are_never_collected(self):
        Host.objects.create(hostname="h4", ip_address="10.30.0.4",
                            agent_token="t4", tags=["", "   ", "real"])
        seen = self._collect()
        self.assertIn("real", seen)
        self.assertNotIn("", seen)
        self.assertNotIn("   ", seen)

    def test_sources_are_recorded_for_reporting(self):
        seen = self._collect()
        self.assertIn("Host.tags", seen["web"]["sources"])
        self.assertIn("Baseline.target_tags", seen["web"]["sources"])


class MembershipUnchangedTests(TestCase):
    """The assertion that actually protects the patching path: creating rows
    must not move a single host into or out of a wave."""

    def test_wave_membership_identical_before_and_after_population(self):
        from apps.tasks.models import wave_host_ids

        a = Host.objects.create(hostname="m1", ip_address="10.31.0.1",
                                agent_token="m1", tags=["Prod"])
        b = Host.objects.create(hostname="m2", ip_address="10.31.0.2",
                                agent_token="m2", tags=["prod"])
        spaced = Host.objects.create(hostname="m3", ip_address="10.31.0.3",
                                     agent_token="m3", tags=["prod "])
        wave = PatchWave.objects.create(name="mw", order=802, tags=["prod"])

        before = set(wave_host_ids(wave))
        for name in ("Prod", "prod", "prod "):
            Tag.get_or_create_by_name(name)
        after = set(wave_host_ids(wave))

        self.assertEqual(before, after, "creating tag rows changed wave membership")
        self.assertEqual(before, {a.id, b.id},
                         "case variants match; the whitespace variant does not")
        self.assertNotIn(spaced.id, after)


class MirrorConsistencyTests(TestCase):
    """The row relations must agree with the string fields they mirror.

    This is the gate on the switch-over: until rows and strings say the same
    thing for every object, flipping the matchers over would change what gets
    patched. Run it against whatever the migration produced.
    """

    def _link(self):
        import importlib

        from django.apps import apps as django_apps

        mod = importlib.import_module("apps.hosts.migrations.0014_link_tag_rows")
        mod.link(django_apps, None)

    def _seed_rows(self):
        import importlib

        from django.apps import apps as django_apps

        mod = importlib.import_module("apps.hosts.migrations.0012_populate_tags")
        mod.populate(django_apps, None)

    def setUp(self):
        self.host = Host.objects.create(
            hostname="mir1", ip_address="10.32.0.1", agent_token="mir1",
            tags=["Prod", "web", "os:debian"])
        self.spaced = Host.objects.create(
            hostname="mir2", ip_address="10.32.0.2", agent_token="mir2",
            tags=["prod "])
        self.wave = PatchWave.objects.create(name="mirw", order=803, tags=["prod"])
        self.baseline = Baseline.objects.create(name="mirb", target_tags=["web"])
        self._seed_rows()
        self._link()

    def test_host_rows_match_its_strings(self):
        keys = {t.key for t in self.host.tag_rows.all()}
        self.assertEqual(keys, {n.lower() for n in self.host.tags})

    def test_whitespace_variant_links_to_its_own_row(self):
        """The spaced host must not end up pointing at the `prod` row — that
        would silently put it in every wave targeting `prod`."""
        rows = list(self.spaced.tag_rows.all())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].key, "prod ")
        self.assertNotIn(rows[0].key, {"prod"})

    def test_case_variants_share_one_row(self):
        prod_row = self.host.tag_rows.get(key="prod")
        self.assertEqual(prod_row.key, "prod")
        # The wave's mirror points at the same row the host does.
        self.assertIn(prod_row.pk, [t.pk for t in self.wave.tag_rows.all()])

    def test_every_mirror_agrees_with_its_strings(self):
        """The blanket assertion — no object may disagree with itself."""
        mismatches = []
        for obj, strings, relation in (
            (self.host, self.host.tags, self.host.tag_rows),
            (self.spaced, self.spaced.tags, self.spaced.tag_rows),
            (self.wave, self.wave.tags, self.wave.tag_rows),
            (self.baseline, self.baseline.target_tags, self.baseline.target_tag_rows),
        ):
            expected = {str(s).lower() for s in strings if str(s).strip()}
            actual = {t.key for t in relation.all()}
            if expected != actual:
                mismatches.append(f"{obj!r}: strings={expected} rows={actual}")
        self.assertEqual(mismatches, [], "row mirror disagrees with string field")

    def test_auto_tags_keep_their_kind_through_linking(self):
        os_row = self.host.tag_rows.get(key="os:debian")
        self.assertEqual(os_row.kind, Tag.Kind.AUTO)

    def test_linking_is_idempotent(self):
        before = {t.pk for t in self.host.tag_rows.all()}
        self._link()
        self.assertEqual({t.pk for t in self.host.tag_rows.all()}, before)
