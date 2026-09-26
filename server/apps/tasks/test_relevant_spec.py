"""Phase 03: the `relevant:` tree — parsed, validated, and signed into the task.

The agent does not evaluate it yet (phase 04); these tests pin the server
contract: grammar, probe validation through the real action path, the
normalised form, risk derivation, input substitution, and the signed params.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from apps.tasks.spec import SpecError, parse_and_validate, resolve_inputs

User = get_user_model()


def yaml_with(body: str) -> str:
    return f"""
name: Probe task
relevant:
{body}
actions:
  - id: noop
    type: run_command
    params: {{ command: "true" }}
"""


class RelevantSpecTests(SimpleTestCase):
    def test_spec_example_parses(self):
        source = yaml_with("""
  all:
    - hunt_package:
        name: openssl
        version_lt: "3.0.13"
  not:
    - hunt_file:
        name: skip-openssl
        paths: /etc/vigil
""")
        spec = parse_and_validate(source)
        # Several keys in one mapping become an all-of the per-key nodes.
        self.assertEqual(spec["relevant"]["op"], "all")
        self.assertEqual([n["op"] for n in spec["relevant"]["items"]], ["all", "not"])
        pkg = spec["relevant"]["items"][0]["items"][0]
        self.assertEqual(pkg["probe"]["id"], "relevant-1")
        self.assertEqual(pkg["probe"]["type"], "hunt_package")
        self.assertEqual(
            pkg["probe"]["params"], {"name": "openssl", "version_lt": "3.0.13"}
        )
        file_probe = spec["relevant"]["items"][1]["items"][0]
        self.assertEqual(file_probe["probe"]["id"], "relevant-2")
        self.assertEqual(file_probe["probe"]["type"], "hunt_file")
        self.assertEqual(
            file_probe["probe"]["params"],
            {"name": "skip-openssl", "paths": "/etc/vigil"},
        )

    def test_no_relevant_is_none(self):
        spec = parse_and_validate(
            "name: Bare\nactions:\n  - id: a\n    type: run_command\n    params: { command: true }\n"
        )
        self.assertIsNone(spec["relevant"])

    def test_any_and_nested(self):
        source = yaml_with("""
  any:
    - hunt_port:
        port: 22
    - all:
        - hunt_file:
            name: a.conf
        - not:
            - hunt_process:
                name: a
""")
        spec = parse_and_validate(source)
        tree = spec["relevant"]
        self.assertEqual(tree["op"], "any")
        ids = [i["probe"]["id"] for i in tree["items"] if "probe" in i]
        self.assertEqual(ids, ["relevant-1"])
        nested = tree["items"][1]
        self.assertEqual(nested["op"], "all")
        self.assertEqual(nested["items"][0]["probe"]["id"], "relevant-2")
        inner = nested["items"][1]
        self.assertEqual(inner["op"], "not")
        self.assertEqual(inner["items"][0]["probe"]["id"], "relevant-3")
        # Depth-first ids count every probe, nested ones included.
        self.assertEqual(len(ids) + 2, 3)

    def test_bad_probes_refused(self):
        cases = {
            "unknown key": yaml_with("""
  all:
    - hunt_nope:
        name: x
"""),
            "non-hunt action": yaml_with("""
  all:
    - run_command:
        command: true
"""),
            "hunt_file with no name or sha256": yaml_with("""
  all:
    - hunt_file:
        paths: /etc
"""),
            "unknown param": yaml_with("""
  all:
    - hunt_package:
        name: openssl
        bogus: 1
"""),
            "empty list": yaml_with("""
  all: []
"""),
            "4 levels deep": yaml_with("""
  all:
    - any:
        - not:
            - all:
                - hunt_file:
                    name: a
"""),
            "11 probes": (
                "name: Probe task\n"
                "relevant:\n"
                "  all:\n"
                + "".join(f"    - hunt_file:\n        name: f{i}\n" for i in range(11))
                + "actions:\n"
                + "  - id: noop\n"
                + "    type: run_command\n"
                + "    params: { command: 'true' }\n"
            ),
        }
        for label, source in cases.items():
            with self.assertRaises(SpecError) as ctx:
                parse_and_validate(source)
            self.assertIn("relevant", str(ctx.exception), label)

    def test_probe_error_names_the_path(self):
        source = yaml_with("""
  not:
    - hunt_nope:
        name: x
""")
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(source)
        self.assertIn("relevant.not[0]: unknown key 'hunt_nope'", str(ctx.exception))

    def test_low_probes_keep_a_low_task_low(self):
        spec = parse_and_validate(
            "name: t\nrisk: low\nrelevant:\n  all:\n    - hunt_process:\n"
            "        name: cron\nactions:\n  - id: c\n    type: check_service\n"
            "    params:\n      service_name: cron\n")
        self.assertEqual(spec["risk"], "low")

    def test_probe_risk_counts(self):
        source = yaml_with("""
  all:
    - hunt_content:
        pattern: secret
        return: text
""")
        spec = parse_and_validate(source)
        self.assertEqual(spec["risk"], "high")

    def test_inputs_reach_probes(self):
        source = (
            "name: Probe task\n"
            "inputs:\n"
            "  - id: pkg\n"
            "relevant:\n"
            "  all:\n"
            "    - hunt_package:\n"
            "        name: ${{ inputs.pkg }}\n"
            "actions:\n"
            "  - id: noop\n"
            "    type: run_command\n"
            "    params: { command: 'true' }\n"
        )
        spec = parse_and_validate(source)
        resolved = resolve_inputs(spec, {"pkg": "openssl"})
        probe = resolved["relevant"]["items"][0]
        self.assertEqual(probe["probe"]["params"]["name"], "openssl")
        self.assertEqual(probe["probe"]["id"], "relevant-1")

    def test_bad_probe_input_ref_refused(self):
        source = (
            "name: Probe task\n"
            "relevant:\n"
            "  all:\n"
            "    - hunt_package:\n"
            "        name: ${{ inputs.undeclared }}\n"
            "actions:\n"
            "  - id: noop\n"
            "    type: run_command\n"
            "    params: { command: 'true' }\n"
        )
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(source)
        self.assertIn("relevant.all[0]", str(ctx.exception))


class RelevantDeployTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="t", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.inputs = {}
        self.host = Host.objects.create(
            hostname="h1",
            agent_token="tok-t1",
            status=Host.Status.ONLINE,
            mode=Host.Mode.FULL_CONTROL,
        )

    def _deploy(self, yaml_source):
        d = TaskDefinition.objects.create(
            owner=self.user,
            name="Relevant task",
            yaml_source=yaml_source,
            parsed_spec=parse_and_validate(yaml_source),
        )
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{d.id}/deploy/",
                {
                    "host_ids": [str(self.host.id)],
                    "totp": "123456",
                    "inputs": self.inputs,
                },
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        return Task.objects.filter(host=self.host).order_by("-created_at").first()

    def test_deploy_signs_the_tree(self):
        self.inputs = {"pkg": "openssl"}
        source = (
            "name: Relevant task\n"
            "inputs:\n"
            "  - id: pkg\n"
            "relevant:\n"
            "  all:\n"
            "    - hunt_package:\n"
            "        name: ${{ inputs.pkg }}\n"
            "  not:\n"
            "    - hunt_file:\n"
            "        name: skip\n"
            "actions:\n"
            "  - id: noop\n"
            "    type: run_command\n"
            "    params: { command: 'true' }\n"
        )
        task = self._deploy(source)
        resolved = resolve_inputs(parse_and_validate(source), {"pkg": "openssl"})
        self.assertEqual(task.params["relevant"], resolved["relevant"])
        # Several keys in the mapping normalise to an all-of the per-key nodes.
        self.assertEqual(task.params["relevant"]["op"], "all")
        pkg_node = task.params["relevant"]["items"][0]
        self.assertEqual(pkg_node["op"], "all")
        self.assertEqual(pkg_node["items"][0]["probe"]["params"]["name"], "openssl")
        self.assertEqual(task.params["steps"][0]["id"], "noop")

    def test_no_relevant_no_key(self):
        source = (
            "name: Bare task\n"
            "actions:\n"
            "  - id: noop\n"
            "    type: run_command\n"
            "    params: { command: 'true' }\n"
        )
        task = self._deploy(source)
        self.assertNotIn("relevant", task.params)
