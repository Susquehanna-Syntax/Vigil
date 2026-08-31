"""Forking a catalog item brings everything it needs with it.

Forking a baseline used to refuse and list what was missing. Honest, but it
left the operator doing the resolution by hand — reading slugs off an error
and hunting each one down in another tab. This walks the graph instead.

The catalog is stubbed rather than fetched: these tests are about the walk and
the dedupe, and a test that needs GitHub to be up is not a test.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.baselines.models import Baseline
from apps.tasks.models import TaskDefinition

TASK_A = """uid: 11111111-1111-4111-8111-111111111111
name: Install Nginx
author: Connor Haggerty
risk: low
actions:
  - type: clear_temp_files
"""
TASK_B = """uid: 22222222-2222-4222-8222-222222222222
name: Clear Temp Files
author: Connor Haggerty
risk: low
actions:
  - type: clear_temp_files
"""
BASELINE = """uid: 33333333-3333-4333-8333-333333333333
name: Standard Build
author: Connor Haggerty
steps:
  - task: install-nginx
    uid: 11111111-1111-4111-8111-111111111111
    order: 1
  - task: clear-temp-files
    uid: 22222222-2222-4222-8222-222222222222
    order: 2
"""
AUTOMATION = """uid: 44444444-4444-4444-8444-444444444444
name: Nightly Build
author: Connor Haggerty
trigger: schedule
cron:
  minute: "0"
  hour: "2"
action_kind: baseline
baseline: standard-build
action_uid: 33333333-3333-4333-8333-333333333333
target: all
"""


def _fake_catalog(kind):
    from apps.tasks.views import _card_for_automation, _card_for_baseline, _card_for_task

    files = {
        "tasks": [("install-nginx.yaml", TASK_A), ("clear-temp-files.yaml", TASK_B)],
        "baselines": [("standard-build.yaml", BASELINE)],
        "automations": [("nightly-build.yaml", AUTOMATION)],
    }[kind]
    card = {"tasks": _card_for_task, "baselines": _card_for_baseline,
            "automations": _card_for_automation}[kind]
    return [{"kind": kind, "filename": name, "html_url": "",
             "yaml_source": text, **card(text)} for name, text in files]


class CascadeForkTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "cf", "cf@example.com", "x")
        self.client.force_login(self.user)
        self.patcher = patch("apps.tasks.views._fetch_community_templates",
                             side_effect=_fake_catalog)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        from django.core.cache import cache
        cache.clear()
        # A migration seeds built-in templates, so counts are measured as
        # deltas rather than against zero.
        self.tasks0 = TaskDefinition.objects.count()
        self.baselines0 = Baseline.objects.count()

    def _counts(self):
        return (TaskDefinition.objects.count() - self.tasks0,
                Baseline.objects.count() - self.baselines0)

    def _fork(self, kind, filename):
        return self.client.post(f"/api/v1/tasks/community/{kind}/{filename}/fork/")

    def _plan(self, kind, filename):
        return self.client.get(f"/api/v1/tasks/community/{kind}/{filename}/plan/")

    # ── The plan ────────────────────────────────────────────────────────────

    def test_the_plan_lists_what_a_baseline_would_pull_in(self):
        needs = self._plan("baselines", "standard-build.yaml").json()["needs"]
        self.assertEqual([n["name"] for n in needs],
                         ["Install Nginx", "Clear Temp Files"])
        self.assertTrue(all(n["found"] for n in needs))
        self.assertFalse(any(n["have"] for n in needs))

    def test_the_plan_marks_what_you_already_have(self):
        self._fork("tasks", "install-nginx.yaml")
        needs = self._plan("baselines", "standard-build.yaml").json()["needs"]
        have = {n["name"]: n["have"] for n in needs}
        self.assertTrue(have["Install Nginx"])
        self.assertFalse(have["Clear Temp Files"])

    def test_an_automation_plan_reaches_through_its_baseline(self):
        """It needs the baseline *and* the baseline's tasks."""
        needs = self._plan("automations", "nightly-build.yaml").json()["needs"]
        self.assertEqual(
            {(n["kind"], n["name"]) for n in needs},
            {("tasks", "Install Nginx"), ("tasks", "Clear Temp Files"),
             ("baselines", "Standard Build")})

    def test_the_plan_creates_nothing(self):
        self._plan("automations", "nightly-build.yaml")
        self.assertEqual(self._counts(), (0, 0))

    # ── The fork ────────────────────────────────────────────────────────────

    def test_forking_a_baseline_brings_its_tasks(self):
        r = self._fork("baselines", "standard-build.yaml")
        self.assertEqual(r.status_code, 201, r.content[:400])
        self.assertEqual(self._counts(), (2, 1))
        baseline = Baseline.objects.get(name="Standard Build")
        self.assertEqual(baseline.steps.count(), 2)

    def test_forking_an_automation_brings_the_whole_graph(self):
        r = self._fork("automations", "nightly-build.yaml")
        self.assertEqual(r.status_code, 201, r.content[:400])
        self.assertEqual(self._counts(), (2, 1))
        from apps.automations.models import Automation
        self.assertEqual(Automation.objects.get(name="Nightly Build").baseline.name,
                         "Standard Build")

    def test_forking_twice_creates_nothing_the_second_time(self):
        """Idempotent by uid — otherwise Fork is a duplicate button.

        Asserting on the *response*, not only on the counts. Counts alone pass
        when the second fork errors on the duplicate name and rolls back, which
        is exactly the bug this is here to catch: nothing was created, but the
        operator was shown a failure for doing something reasonable.
        """
        self._fork("baselines", "standard-build.yaml")
        before = self._counts()
        second = self._fork("baselines", "standard-build.yaml")
        self.assertEqual(second.status_code, 200, second.content[:300])
        self.assertTrue(second.json().get("already"),
                        "a second fork must be a clean no-op, not an error")
        self.assertEqual(second.json()["created"], [])
        self.assertEqual(self._counts(), before)

    def test_a_forked_baseline_records_the_catalog_uid(self):
        """What makes the no-op above possible."""
        self._fork("baselines", "standard-build.yaml")
        self.assertEqual(str(Baseline.objects.get(name="Standard Build").community_uid),
                         "33333333-3333-4333-8333-333333333333")

    def test_a_forked_automation_records_the_catalog_uid(self):
        from apps.automations.models import Automation

        self._fork("automations", "nightly-build.yaml")
        self.assertEqual(
            str(Automation.objects.get(name="Nightly Build").community_uid),
            "44444444-4444-4444-8444-444444444444")

    def test_re_forking_an_automation_is_also_a_clean_no_op(self):
        self._fork("automations", "nightly-build.yaml")
        second = self._fork("automations", "nightly-build.yaml")
        self.assertEqual(second.status_code, 200, second.content[:300])
        self.assertTrue(second.json().get("already"))

    def test_a_renamed_baseline_is_still_recognised(self):
        """Renaming your copy must not make Fork offer it again."""
        self._fork("baselines", "standard-build.yaml")
        baseline = Baseline.objects.get(name="Standard Build")
        baseline.name = "My Build"
        baseline.save()
        second = self._fork("baselines", "standard-build.yaml")
        self.assertEqual(second.status_code, 200, second.content[:300])
        self.assertTrue(second.json().get("already"))
        self.assertEqual(Baseline.objects.filter(community_uid=baseline.community_uid).count(), 1)

    def test_a_task_you_already_hold_is_not_duplicated(self):
        self._fork("tasks", "install-nginx.yaml")
        self._fork("baselines", "standard-build.yaml")
        self.assertEqual(
            TaskDefinition.objects.filter(name="Install Nginx").count(), 1)

    def test_the_fork_records_the_catalog_uid(self):
        """Without this the second fork would duplicate after a rename."""
        self._fork("tasks", "install-nginx.yaml")
        definition = TaskDefinition.objects.get(name="Install Nginx")
        self.assertEqual(str(definition.community_uid),
                         "11111111-1111-4111-8111-111111111111")

    def test_a_renamed_local_copy_is_still_recognised(self):
        """The reason uids exist. Rename your copy, re-fork the baseline, and
        it must reuse yours rather than making a second one."""
        self._fork("tasks", "install-nginx.yaml")
        definition = TaskDefinition.objects.get(name="Install Nginx")
        definition.name = "My Renamed Nginx Task"
        definition.save()

        self._fork("baselines", "standard-build.yaml")
        self.assertEqual(self._counts(), (2, 1),
                         "the renamed copy should have been reused, not re-forked")
        baseline = Baseline.objects.get(name="Standard Build")
        self.assertIn(definition.id,
                      [s.definition_id for s in baseline.steps.all()])

    def test_two_tasks_may_share_a_name(self):
        """Names are not unique locally, and the uid is what keeps that safe."""
        TaskDefinition.objects.create(
            name="Install Nginx", owner=self.user,
            yaml_source="name: Install Nginx\nrisk: low\nactions:\n  - type: clear_temp_files\n",
            parsed_spec={"name": "Install Nginx", "actions": []}, risk_level="low")
        self._fork("tasks", "install-nginx.yaml")
        # The pre-existing one has no uid, so the catalog copy is a different
        # thing that happens to share a name — both survive.
        self.assertEqual(
            TaskDefinition.objects.filter(name="Install Nginx").count(), 2)

    def test_an_unknown_file_is_a_404(self):
        self.assertEqual(self._fork("baselines", "nope.yaml").status_code, 404)


class UidBeatsNameTests(CascadeForkTests):
    """When a catalog file carries a uid, the name is not consulted at all.

    Falling back to the name would undo what uids are for: two unrelated things
    may share one, and treating the operator's own "Install Nginx" as the
    catalog's would skip the fork and leave a baseline pointing at steps its
    author never wrote.
    """

    def _local_lookalike(self):
        return TaskDefinition.objects.create(
            name="Install Nginx", owner=self.user,
            yaml_source="name: Install Nginx\nrisk: low\nactions:\n  - type: clear_temp_files\n",
            parsed_spec={"name": "Install Nginx", "actions": []},
            risk_level="low")

    def test_a_same_named_local_task_does_not_satisfy_a_uid_reference(self):
        mine = self._local_lookalike()
        needs = self._plan("baselines", "standard-build.yaml").json()["needs"]
        have = {n["name"]: n["have"] for n in needs}
        self.assertFalse(have["Install Nginx"],
                         "a name collision must not pass for the catalog's task")

        self._fork("baselines", "standard-build.yaml")
        catalog_copy = TaskDefinition.objects.get(
            community_uid="11111111-1111-4111-8111-111111111111")
        self.assertNotEqual(catalog_copy.id, mine.id)
        baseline = Baseline.objects.get(name="Standard Build")
        self.assertIn(catalog_copy.id,
                      [s.definition_id for s in baseline.steps.all()])
        self.assertNotIn(mine.id,
                         [s.definition_id for s in baseline.steps.all()])

    def test_the_uid_bearing_copy_is_still_deduped(self):
        """Uid matching must keep working with the lookalike present."""
        self._local_lookalike()
        self._fork("baselines", "standard-build.yaml")
        before = self._counts()
        self._fork("baselines", "standard-build.yaml")
        self.assertEqual(self._counts(), before)
