"""Baselines and automations as community YAML: export, import, round-trip.

The contract these pin is that the server and the Vigil-Approved-Scripts repo
agree about the dialect. The repo is the authority — its validators run in its
own CI — so what matters here is that what we export would pass them, and that
what it publishes we can read back.
"""

from datetime import date

import yaml
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations import community_yaml as auto_yaml
from apps.automations.models import Automation
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate
from vigil.contentyaml import ContentYamlError, slugify

from . import community_yaml as bl_yaml
from .models import Baseline, BaselineStep


def _definition(user, name):
    src = f"name: {name}\nrisk: low\nactions:\n  - type: clear_temp_files\n"
    return TaskDefinition.objects.create(
        name=name, owner=user, yaml_source=src,
        parsed_spec=parse_and_validate(src), risk_level="low")


class SlugTests(TestCase):
    def test_slug_matches_the_repos_existing_filenames(self):
        """These are real files in Vigil-Approved-Scripts. If this drifts, an
        export lands under a name that references nothing."""
        cases = {
            "Standard Linux Server Build": "standard-linux-server-build",
            "Container Host Maintenance": "container-host-maintenance",
            "Prune Docker On Low Disk": "prune-docker-on-low-disk",
            "Install Nginx": "install-nginx",
            "Clear Temp Files": "clear-temp-files",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(slugify(name), expected)

    def test_punctuation_collapses_to_single_hyphens(self):
        self.assertEqual(slugify("Restart nginx  &&  verify!!"),
                         "restart-nginx-verify")

    def test_a_name_of_only_punctuation_falls_back(self):
        self.assertEqual(slugify("***", fallback="task"), "task")

    def test_the_slug_is_capped_at_sixty_characters(self):
        """At most 60 — trimming a hyphen the cut left behind can make it 59."""
        for n in (10, 20, 40, 80):
            with self.subTest(n=n):
                self.assertLessEqual(len(slugify("word " * n)), 60)
        self.assertGreaterEqual(len(slugify("word " * 40)), 55)

    def test_the_cap_never_leaves_a_trailing_hyphen(self):
        """A trailing hyphen would resolve to a file nobody named that way."""
        for n in range(1, 40):
            with self.subTest(n=n):
                self.assertFalse(slugify("ab " * n).endswith("-"))


class BaselineYamlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "cy", "cy@example.com", "x")
        self.a = _definition(self.user, "Install Nginx")
        self.b = _definition(self.user, "Clear Temp Files")
        self.baseline = Baseline.objects.create(
            name="Standard Linux Server Build",
            description="Bring a fresh host to a known state.",
            target_tags=["linux", "server"], created_by=self.user)
        BaselineStep.objects.create(baseline=self.baseline, definition=self.a, order=1)
        BaselineStep.objects.create(baseline=self.baseline, definition=self.b, order=2,
                                    params_override={"0": {"older_than_days": 3}})

    def test_export_names_tasks_by_slug(self):
        parsed = yaml.safe_load(bl_yaml.to_yaml(self.baseline, author="Connor Haggerty"))
        self.assertEqual([s["task"] for s in parsed["steps"]],
                         ["install-nginx", "clear-temp-files"])

    def test_export_emits_the_repos_field_order(self):
        """A reviewer reading a diff should see the same shape every time."""
        text = bl_yaml.to_yaml(self.baseline, author="Connor Haggerty")
        keys = [line.split(":")[0] for line in text.splitlines()
                if line and not line.startswith((" ", "-"))]
        # Identity first, then attribution, then prose, then the steps.
        self.assertEqual(keys[:4], ["name", "uid", "author", "description"])
        self.assertEqual(keys[-1], "steps")

    def test_export_omits_allow_high_risk_when_false(self):
        self.assertNotIn("allow_high_risk", bl_yaml.to_yaml(self.baseline))

    def test_export_states_allow_high_risk_when_true(self):
        self.baseline.allow_high_risk = True
        self.assertIn("allow_high_risk: true", bl_yaml.to_yaml(self.baseline))

    def test_created_is_emitted_unquoted(self):
        """Every file in the repo has a bare date; a quoted one is a diff."""
        text = bl_yaml.to_yaml(self.baseline, created=date(2026, 8, 29))
        self.assertIn("created: 2026-08-29", text)

    def test_round_trip_preserves_every_field(self):
        parsed = bl_yaml.parse(
            bl_yaml.to_yaml(self.baseline, author="Connor Haggerty"))
        self.assertEqual(parsed["name"], self.baseline.name)
        self.assertEqual(parsed["description"], self.baseline.description)
        self.assertEqual(parsed["target_tags"], ["linux", "server"])
        self.assertEqual(len(parsed["steps"]), 2)
        self.assertEqual(parsed["steps"][1]["params_override"],
                         {"0": {"older_than_days": 3}})

    def test_resolve_steps_maps_slugs_back_to_definitions(self):
        parsed = bl_yaml.parse(bl_yaml.to_yaml(self.baseline))
        resolved = bl_yaml.resolve_steps(parsed["steps"],
                                         TaskDefinition.objects.all())
        self.assertEqual([s["definition"].id for s in resolved],
                         [self.a.id, self.b.id])

    def test_resolve_steps_names_every_missing_task_at_once(self):
        """Fixing a twelve-step baseline one error at a time is miserable."""
        parsed = bl_yaml.parse(
            "name: X\nsteps:\n  - task: not-here\n  - task: also-missing\n")
        with self.assertRaises(ContentYamlError) as caught:
            bl_yaml.resolve_steps(parsed["steps"], TaskDefinition.objects.all())
        self.assertIn("not-here", str(caught.exception))
        self.assertIn("also-missing", str(caught.exception))

    def test_duplicate_orders_are_refused(self):
        """Mirrors the unique constraint, with a readable message."""
        with self.assertRaises(ContentYamlError) as caught:
            bl_yaml.parse("name: X\nsteps:\n"
                          "  - task: a\n    order: 1\n"
                          "  - task: b\n    order: 1\n")
        self.assertIn("order", str(caught.exception))

    def test_a_baseline_with_no_steps_is_refused(self):
        """It would import as a baseline that silently does nothing."""
        with self.assertRaises(ContentYamlError):
            bl_yaml.parse("name: X\nsteps: []\n")

    def test_a_placeholder_author_is_refused(self):
        for author in ("Admin", "TODO", "unknown"):
            with self.subTest(author=author):
                with self.assertRaises(ContentYamlError):
                    bl_yaml.parse(
                        f"name: X\nauthor: {author}\nsteps:\n  - task: a\n")

    def test_non_mapping_yaml_is_refused(self):
        for text in ("- just\n- a\n- list\n", "just a string\n", ""):
            with self.subTest(text=text[:12]):
                with self.assertRaises(ContentYamlError):
                    bl_yaml.parse(text)


class AutomationYamlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "ay", "ay@example.com", "x")
        self.definition = _definition(self.user, "Clear Temp Files")
        self.baseline = Baseline.objects.create(
            name="Container Host Maintenance", created_by=self.user)
        BaselineStep.objects.create(baseline=self.baseline,
                                    definition=self.definition, order=1)

    def _automation(self, **kw):
        defaults = dict(
            name="Prune Docker On Low Disk", trigger="event",
            event="alert_fired", min_severity="warning",
            action_kind="baseline", baseline=self.baseline,
            target="event_host", created_by=self.user)
        return Automation.objects.create(**{**defaults, **kw})

    def test_round_trip_of_an_event_automation(self):
        parsed = auto_yaml.parse(auto_yaml.to_yaml(self._automation()))
        self.assertEqual(parsed["trigger"], "event")
        self.assertEqual(parsed["event"], "alert_fired")
        self.assertEqual(parsed["min_severity"], "warning")
        self.assertEqual(parsed["action_kind"], "baseline")
        self.assertEqual(parsed["slug"], "container-host-maintenance")

    def test_round_trip_of_a_scheduled_automation(self):
        automation = self._automation(
            trigger="schedule", event="", target="tags", target_tags=["docker"],
            cron_minute="30", cron_hour="3", cron_dow="1")
        parsed = auto_yaml.parse(auto_yaml.to_yaml(automation))
        self.assertEqual(parsed["cron"]["minute"], "30")
        self.assertEqual(parsed["cron"]["hour"], "3")
        self.assertEqual(parsed["cron"]["dow"], "1")
        self.assertEqual(parsed["target_tags"], ["docker"])

    def test_text_filters_survive_the_round_trip(self):
        automation = self._automation(
            match_text="/var", match_field="message", match_mode="contains")
        parsed = auto_yaml.parse(auto_yaml.to_yaml(automation))
        self.assertEqual(parsed["match_text"], "/var")
        self.assertEqual(parsed["match_field"], "message")

    def test_a_host_targeted_automation_refuses_to_export(self):
        """A host id means nothing on someone else's server, so this is
        unrepresentable rather than merely lossy."""
        from apps.hosts.models import Host

        host = Host.objects.create(hostname="h1", ip_address="10.0.0.5",
                                   agent_token="t" * 32)
        automation = self._automation(target="host", target_host=host)
        with self.assertRaises(ContentYamlError) as caught:
            auto_yaml.to_yaml(automation)
        self.assertIn("specific host", str(caught.exception))

    def test_a_host_watching_automation_refuses_to_export(self):
        from apps.hosts.models import Host

        host = Host.objects.create(hostname="h2", ip_address="10.0.0.6",
                                   agent_token="u" * 32)
        automation = self._automation(event_host=host)
        with self.assertRaises(ContentYamlError):
            auto_yaml.to_yaml(automation)

    def test_target_host_is_refused_on_import(self):
        with self.assertRaises(ContentYamlError):
            auto_yaml.parse("name: X\ntrigger: event\nevent: alert_fired\n"
                            "action_kind: task\ntask: t\ntarget: host\n")

    def test_a_cron_field_that_is_not_a_cron_field_is_refused(self):
        """These strings reach a scheduler."""
        for bad in ("0; rm -rf /", "* $(whoami)", "`id`", "0 && echo"):
            with self.subTest(bad=bad):
                with self.assertRaises(ContentYamlError):
                    auto_yaml.parse(
                        "name: X\ntrigger: schedule\naction_kind: task\n"
                        f"task: t\ntarget: all\ncron:\n  minute: '{bad}'\n")

    def test_an_integer_cron_field_is_accepted(self):
        """`minute: 0` is the natural thing to write and YAML makes it an int."""
        parsed = auto_yaml.parse(
            "name: X\ntrigger: schedule\naction_kind: task\ntask: t\n"
            "target: all\ncron:\n  minute: 0\n  hour: 3\n")
        self.assertEqual(parsed["cron"]["minute"], "0")
        self.assertEqual(parsed["cron"]["hour"], "3")

    def test_target_tags_must_be_present_for_a_tag_target(self):
        """Otherwise it would match no hosts and look like it works."""
        with self.assertRaises(ContentYamlError):
            auto_yaml.parse("name: X\ntrigger: event\nevent: alert_fired\n"
                            "action_kind: task\ntask: t\ntarget: tags\n")

    def test_naming_both_a_task_and_a_baseline_is_refused(self):
        with self.assertRaises(ContentYamlError):
            auto_yaml.parse("name: X\ntrigger: event\nevent: alert_fired\n"
                            "action_kind: task\ntask: t\nbaseline: b\n")


class SlugIsAFilenameTests(TestCase):
    """A community slug is a filename, not ``slugify(name)``.

    The repo does not enforce that the two agree, and one file already differs:
    ``docker-prune-and-restart-unhealthy.yaml`` holds a task called "Docker
    Prune and Restart Unhealthy Container". Resolving by slugifying library
    names alone refuses to import a baseline whose task the operator has.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "sf", "sf@example.com", "x")
        self.definition = _definition(
            self.user, "Docker Prune and Restart Unhealthy Container")
        self.index = {"docker-prune-and-restart-unhealthy":
                      "Docker Prune and Restart Unhealthy Container"}

    def _steps(self):
        return bl_yaml.parse(
            "name: Uses It\nsteps:\n"
            "  - task: docker-prune-and-restart-unhealthy\n")["steps"]

    def test_without_the_index_the_slug_does_not_resolve(self):
        """Documents why the second pass exists."""
        with self.assertRaises(ContentYamlError):
            bl_yaml.resolve_steps(self._steps(), TaskDefinition.objects.all())

    def test_the_index_resolves_it(self):
        resolved = bl_yaml.resolve_steps(
            self._steps(), TaskDefinition.objects.all(), self.index)
        self.assertEqual(resolved[0]["definition"].id, self.definition.id)

    def test_an_index_miss_still_reports_the_slug(self):
        with self.assertRaises(ContentYamlError) as caught:
            bl_yaml.resolve_steps(self._steps(), TaskDefinition.objects.all(),
                                  {"something-else": "Other"})
        self.assertIn("docker-prune-and-restart-unhealthy", str(caught.exception))

    def test_an_empty_index_is_not_an_error(self):
        """The index comes from the network. Losing it must degrade to the
        first pass, not raise."""
        with self.assertRaises(ContentYamlError):
            bl_yaml.resolve_steps(self._steps(), TaskDefinition.objects.all(), {})

    def test_the_exact_slug_still_wins_over_the_index(self):
        """A library task whose own name slugifies to the wanted slug is the
        better match, and must not be shadowed by an index entry."""
        exact = _definition(self.user, "Docker Prune And Restart Unhealthy")
        resolved = bl_yaml.resolve_steps(
            self._steps(), TaskDefinition.objects.all(), self.index)
        self.assertEqual(resolved[0]["definition"].id, exact.id)

    def test_automations_resolve_the_same_way(self):
        parsed = auto_yaml.parse(
            "name: A\ntrigger: event\nevent: alert_fired\naction_kind: task\n"
            "task: docker-prune-and-restart-unhealthy\n")
        definition, baseline = auto_yaml.resolve_action(
            parsed, definitions=TaskDefinition.objects.all(), baselines=[],
            task_names_by_slug=self.index)
        self.assertEqual(definition.id, self.definition.id)
        self.assertIsNone(baseline)


class ZeroBasedOrderTests(TestCase):
    """A baseline created through the UI stores 0-based step orders.

    The community schema's ``order`` is 1-based, so exporting the stored value
    emitted ``order: 0`` and the export failed to import — through this
    module's own parser. Every fixture above sets explicit 1-based orders,
    which is exactly why none of them caught it.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "zb", "zb@example.com", "x")
        self.baseline = Baseline.objects.create(name="Zero Based",
                                                created_by=self.user)
        for i, name in enumerate(("Install Nginx", "Clear Temp Files")):
            BaselineStep.objects.create(
                baseline=self.baseline, definition=_definition(self.user, name),
                order=i)   # 0-based, as _validate_and_set_steps writes them

    def test_export_numbers_steps_from_one(self):
        parsed = yaml.safe_load(bl_yaml.to_yaml(self.baseline))
        self.assertEqual([s["order"] for s in parsed["steps"]], [1, 2])

    def test_the_export_can_be_imported_again(self):
        """The round trip that was broken."""
        reparsed = bl_yaml.parse(bl_yaml.to_yaml(self.baseline))
        self.assertEqual(len(reparsed["steps"]), 2)

    def test_step_sequence_survives_the_round_trip(self):
        reparsed = bl_yaml.parse(bl_yaml.to_yaml(self.baseline))
        self.assertEqual([s["task"] for s in reparsed["steps"]],
                         ["install-nginx", "clear-temp-files"])
