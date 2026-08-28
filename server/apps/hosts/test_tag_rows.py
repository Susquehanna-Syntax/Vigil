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


class WriteSyncTests(TestCase):
    """Strings stay the write interface; rows follow automatically on save.

    Six different places assign `host.tags`, so converting every caller would
    be a losing game. The sync happens on save instead, which catches all of
    them — including the check-in path that runs for every host every minute.
    """

    def test_creating_a_host_creates_its_rows(self):
        host = Host.objects.create(hostname="ws1", ip_address="10.33.0.1",
                                   agent_token="ws1", tags=["alpha", "beta"])
        self.assertEqual({t.key for t in host.tag_rows.all()}, {"alpha", "beta"})

    def test_adding_a_tag_adds_a_row(self):
        host = Host.objects.create(hostname="ws2", ip_address="10.33.0.2",
                                   agent_token="ws2", tags=["alpha"])
        host.tags = ["alpha", "gamma"]
        host.save()
        self.assertEqual({t.key for t in host.tag_rows.all()}, {"alpha", "gamma"})

    def test_removing_a_tag_removes_the_link_but_keeps_the_row(self):
        """The Tag row survives — other hosts may still use it, and deleting
        it here would make tag history disappear under them."""
        host = Host.objects.create(hostname="ws3", ip_address="10.33.0.3",
                                   agent_token="ws3", tags=["alpha", "beta"])
        host.tags = ["alpha"]
        host.save()
        self.assertEqual({t.key for t in host.tag_rows.all()}, {"alpha"})
        self.assertTrue(Tag.objects.filter(key="beta").exists())

    def test_case_change_does_not_create_a_second_row(self):
        host = Host.objects.create(hostname="ws4", ip_address="10.33.0.4",
                                   agent_token="ws4", tags=["Alpha"])
        host.tags = ["alpha"]
        host.save()
        self.assertEqual(Tag.objects.filter(key="alpha").count(), 1)

    def test_whitespace_variant_creates_a_second_row(self):
        """Consistent with the migration: `alpha ` is a different tag."""
        host = Host.objects.create(hostname="ws5", ip_address="10.33.0.5",
                                   agent_token="ws5", tags=["alpha", "alpha "])
        self.assertEqual({t.key for t in host.tag_rows.all()}, {"alpha", "alpha "})

    def test_blank_tags_never_become_rows(self):
        host = Host.objects.create(hostname="ws6", ip_address="10.33.0.6",
                                   agent_token="ws6", tags=["", "  ", "real"])
        self.assertEqual({t.key for t in host.tag_rows.all()}, {"real"})

    def test_unchanged_tags_do_no_writes(self):
        """This runs on every check-in, so an unchanged save must be cheap."""
        host = Host.objects.create(hostname="ws7", ip_address="10.33.0.7",
                                   agent_token="ws7", tags=["alpha"])
        from apps.hosts.models import sync_tag_rows
        self.assertFalse(sync_tag_rows(host, "tags", "tag_rows"),
                         "an unchanged save should report no change")

    def test_wave_and_baseline_sync_too(self):
        wave = PatchWave.objects.create(name="wsw", order=804, tags=["canary"])
        self.assertEqual({t.key for t in wave.tag_rows.all()}, {"canary"})
        baseline = Baseline.objects.create(name="wsb", target_tags=["web"])
        self.assertEqual({t.key for t in baseline.target_tag_rows.all()}, {"web"})

    def test_automation_syncs_both_of_its_lists(self):
        auto = Automation.objects.create(
            name="wsa", trigger=Automation.Trigger.EVENT, event="alert_fired",
            action_kind=Automation.ActionKind.TASK,
            event_tags=["evt"], target_tags=["tgt"])
        self.assertEqual({t.key for t in auto.event_tag_rows.all()}, {"evt"})
        self.assertEqual({t.key for t in auto.target_tag_rows.all()}, {"tgt"})


class TagApiTests(TestCase):
    """The Tags tab's backend. Reads open so every picker can populate; writes
    admin-only, because a tag decides which machines a wave patches."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.admin = User.objects.create_user("tagadmin", password="x",
                                              is_staff=True, is_superuser=True)
        self.viewer = User.objects.create_user("tagviewer", password="x")
        self.host = Host.objects.create(hostname="ta1", ip_address="10.34.0.1",
                                        agent_token="ta1", tags=["prod", "os:debian"])

    def test_list_shows_usage_counts(self):
        self.client.force_login(self.viewer)
        rows = {t["name"]: t for t in self.client.get("/api/v1/tags/").json()}
        self.assertEqual(rows["prod"]["host_count"], 1)

    def test_auto_tags_are_listed_but_not_editable(self):
        self.client.force_login(self.viewer)
        rows = {t["name"]: t for t in self.client.get("/api/v1/tags/").json()}
        self.assertFalse(rows["os:debian"]["editable"])
        self.assertTrue(rows["prod"]["editable"])

    def test_viewer_cannot_create(self):
        self.client.force_login(self.viewer)
        r = self.client.post("/api/v1/tags/", {"name": "new"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_admin_creates(self):
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/tags/", {"name": "staging"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(Tag.objects.filter(key="staging").exists())

    def test_reserved_prefixes_are_refused(self):
        """os:/pkg:/arch: are rebuilt from inventory and agent:* exists so a
        compromised agent cannot impersonate an operator tag."""
        self.client.force_login(self.admin)
        for name in ("os:ubuntu", "agent:spoofed", "pkg:apt", "arch:arm64"):
            with self.subTest(name=name):
                r = self.client.post("/api/v1/tags/", {"name": name},
                                     content_type="application/json")
                self.assertEqual(r.status_code, 400)

    def test_duplicate_by_case_is_refused(self):
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/tags/", {"name": "PROD"},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_whitespace_variant_is_allowed_as_a_separate_tag(self):
        """Consistent with the migration: `prod ` never matched `prod`."""
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/tags/", {"name": "prod "},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)

    def test_rename_updates_the_string_mirrors_too(self):
        """The point of rows: one rename lands everywhere."""
        wave = PatchWave.objects.create(name="tw", order=805, tags=["prod"])
        tag = Tag.objects.get(key="prod")
        self.client.force_login(self.admin)
        r = self.client.patch(f"/api/v1/tags/{tag.id}/", {"name": "production"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.host.refresh_from_db()
        wave.refresh_from_db()
        self.assertIn("production", self.host.tags)
        self.assertNotIn("prod", self.host.tags)
        self.assertIn("production", wave.tags)

    def test_rename_keeps_wave_membership(self):
        from apps.tasks.models import wave_host_ids

        wave = PatchWave.objects.create(name="tw2", order=806, tags=["prod"])
        before = set(wave_host_ids(wave))
        tag = Tag.objects.get(key="prod")
        self.client.force_login(self.admin)
        self.client.patch(f"/api/v1/tags/{tag.id}/", {"name": "production"},
                          content_type="application/json")
        wave.refresh_from_db()
        self.assertEqual(set(wave_host_ids(wave)), before,
                         "renaming a tag must not move hosts between waves")

    def test_auto_tag_cannot_be_renamed(self):
        tag = Tag.objects.get(key="os:debian")
        self.client.force_login(self.admin)
        r = self.client.patch(f"/api/v1/tags/{tag.id}/", {"name": "os:other"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_delete_is_refused_while_in_use(self):
        """Deleting a tag a wave selects on would silently empty that wave."""
        PatchWave.objects.create(name="tw3", order=807, tags=["prod"])
        tag = Tag.objects.get(key="prod")
        self.client.force_login(self.admin)
        r = self.client.delete(f"/api/v1/tags/{tag.id}/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("still used by", r.json()["detail"])

    def test_delete_works_once_unused(self):
        self.client.force_login(self.admin)
        self.client.post("/api/v1/tags/", {"name": "orphan"},
                         content_type="application/json")
        tag = Tag.objects.get(key="orphan")
        self.assertEqual(self.client.delete(f"/api/v1/tags/{tag.id}/").status_code, 204)
