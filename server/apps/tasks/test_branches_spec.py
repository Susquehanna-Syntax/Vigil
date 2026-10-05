"""Phase 05: if/then/else — parsed, validated, risk-rated and signed (server).

A task's ``actions:`` may branch on an earlier step's result. The whole tree
is validated and signed, including branches that will not run; the task's
risk is the highest across all branches. The agent learns to follow the
branches in phase 06 — until then the server must never hand a branching
task to any agent (the check-in feature gate).
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from apps.tasks.spec import SpecError, parse_and_validate

User = get_user_model()

SPEC_YAML = """
name: Fix nginx
risk: high
actions:
  - id: svc
    type: hunt_service
    params: {name: nginx}
  - if: steps.svc.result.count > 0
    then:
      - id: stop
        type: stop_service
        params: {service_name: nginx}
      - id: upd
        type: update_package
        params: {package_name: openssl}
    else:
      - id: upd2
        type: update_package
        params: {package_name: openssl}
"""

_CHECKIN = "/api/v1/checkin"


def _yaml(steps: str) -> str:
    return (
        "name: Branches\n"
        "actions:\n"
        + steps
    )


def _error(source: str) -> str:
    try:
        parse_and_validate(source)
    except SpecError as exc:
        return str(exc)
    raise AssertionError(f"SpecError not raised for: {source!r}")


class BranchSpecTests(SimpleTestCase):
    def test_spec_example_parses(self):
        spec = parse_and_validate(SPEC_YAML)
        self.assertEqual(
            [a["id"] for a in spec["actions"]], ["svc", "stop", "upd", "upd2"])
        self.assertEqual(
            [a["branch"] for a in spec["actions"]],
            [None, "b1.then", "b1.then", "b1.else"])
        self.assertEqual(
            [node["step"] if "step" in node else node["id"]
             for node in spec["flow"]],
            ["svc", "b1"])
        b1 = spec["flow"][1]
        self.assertEqual(b1["if"], "steps.svc.result.count > 0")
        self.assertEqual(
            [n["step"] for n in b1["then"]], ["stop", "upd"])
        self.assertEqual(
            [n["step"] for n in b1["else"]], ["upd2"])

    def test_flow_names_steps_by_id_only(self):
        # The steps are signed once, in params["steps"], with inputs
        # resolved; the flow must not carry a second, unresolved copy.
        spec = parse_and_validate(SPEC_YAML)
        self.assertNotIn("params", repr(spec["flow"]))

    def test_nested_branch_condition_is_validated(self):
        msg = _error(
            "name: N\nactions:\n"
            "  - id: a\n    type: check_service\n    params: {service_name: cron}\n"
            "  - if: steps.a.status == \"ok\"\n    then:\n"
            "      - if: steps.nope.status == \"ok\"\n        then:\n"
            "          - id: b\n            type: check_service\n"
            "            params: {service_name: cron}\n")
        self.assertIn("branch b2", msg)
        self.assertIn("steps.nope", msg)

    def test_no_branch_flow_is_none(self):
        spec = parse_and_validate(
            "name: Bare\nactions:\n"
            "  - id: a\n    type: check_service\n"
            "    params: {service_name: cron}\n")
        self.assertIsNone(spec["flow"])
        self.assertEqual(spec["actions"][0]["branch"], None)

    def test_risk_is_highest_across_branches(self):
        source = _yaml("""
  - id: low
    type: check_service
    params: {service_name: cron}
  - if: agent.os == 'linux'
    then:
      - id: low2
        type: check_service
        params: {service_name: cron}
    else:
      - id: boom
        type: delete_path
        params: {path: '/var/tmp/scratch'}
""")
        spec = parse_and_validate(source)
        self.assertEqual(spec["risk"], "high")
        self.assertEqual(spec["actions"][2]["branch"], "b1.else")

    def test_nesting_and_step_limits(self):
        def branch(expr, inner):
            indented = "\n".join(
                ("    " if line else line) + line for line in inner.strip("\n").splitlines())
            return f"  - if: {expr}\n    then:\n{indented}\n"

        inner = "  - id: leaf\n    type: check_service\n    params: {service_name: cron}\n"
        for _ in range(4):
            inner = branch("agent.os == 'linux'", inner)
        deep = "name: Deep\nactions:\n" + inner
        err = _error(deep)
        self.assertIn("nested deeper than 3", err)

        # 51 leaves across branches (each in its own then of a nested if is
        # fine for depth — build them flat in one branch).
        leaves = "".join(
            f"      - id: s{i}\n        type: check_service\n"
            f"        params: {{service_name: c{i}}}\n" for i in range(51))
        too_many = (
            "name: Many\nactions:\n"
            "  - id: root\n    type: check_service\n"
            "    params: {service_name: cron}\n"
            "  - if: steps.root.status == 'ok'\n"
            "    then:\n"
            + leaves
        )
        err = _error(too_many)
        self.assertIn("too many steps across all branches", err)

        # 40 leaves OK.
        leaves = "".join(
            f"      - id: s{i}\n        type: check_service\n"
            f"        params: {{service_name: c{i}}}\n" for i in range(40))
        ok = (
            "name: Many\nactions:\n"
            "  - id: root\n    type: check_service\n"
            "    params: {service_name: cron}\n"
            "  - if: steps.root.status == 'ok'\n"
            "    then:\n"
            + leaves
        )
        spec = parse_and_validate(ok)
        self.assertEqual(len(spec["actions"]), 41)

    def test_bad_branch_shapes(self):
        # Missing then.
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.status == 'ok'
"""))
        self.assertIn("then", err)
        # Empty then.
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.status == 'ok'
    then: []
"""))
        self.assertIn("then", err)
        # Extra key.
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.status == 'ok'
    then:
      - id: b
        type: check_service
        params: {service_name: cron}
    why: no
"""))
        self.assertIn("unknown key", err)
        # Non-string if.
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: 1
    then:
      - id: b
        type: check_service
        params: {service_name: cron}
"""))
        self.assertIn("if must be a non-empty string", err)
        # type: playbook inside a branch.
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.status == 'ok'
    then:
      - id: pb
        type: playbook
        params: {name: some playbook}
"""))
        self.assertIn("playbook", err)

    def test_if_refs_must_be_earlier(self):
        # Ref to a step inside the same branch.
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.b.status == 'ok'
    then:
      - id: b
        type: check_service
        params: {service_name: cron}
"""))
        self.assertIn("not an earlier step", err)
        # Ref to an earlier top-level step is fine.
        parse_and_validate(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.result.active == True
    then:
      - id: b
        type: check_service
        params: {service_name: cron}
"""))

    def test_ordering_on_str_or_bool_output_refused(self):
        # check_service outputs `active` (bool) and `state` (str).
        base = _yaml("""
  - id: svc
    type: check_service
    params: {service_name: cron}
  - if: steps.svc.result.active > 0
    then:
      - id: b
        type: check_service
        params: {service_name: cron}
""")
        err = _error(base)
        self.assertIn("is a bool output", err)
        base = base.replace("steps.svc.result.active > 0",
                            "steps.svc.result.state > 0")
        err = _error(base)
        self.assertIn("is a str output", err)
        # when: gets the same check.
        base = _yaml("""
  - id: svc
    type: check_service
    params: {service_name: cron}
  - id: b
    type: check_service
    when: steps.svc.result.active > 0
    params: {service_name: cron}
""")
        err = _error(base)
        self.assertIn("is a bool output", err)
        # A numeric output still compares.
        parse_and_validate(_yaml("""
  - id: svc
    type: hunt_service
    params: {name: nginx}
  - if: steps.svc.result.count > 0
    then:
      - id: b
        type: check_service
        params: {service_name: cron}
"""))

    def test_ids_unique_across_branches(self):
        err = _error(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.status == 'ok'
    then:
      - id: a
        type: check_service
        params: {service_name: cron}
"""))
        self.assertIn("duplicate action id", err)

    def test_nested_branch_paths(self):
        spec = parse_and_validate(_yaml("""
  - id: a
    type: check_service
    params: {service_name: cron}
  - if: steps.a.status == 'ok'
    then:
      - if: steps.a.result.active == True
        then:
          - id: c
            type: check_service
            params: {service_name: cron}
        else:
          - id: d
            type: check_service
            params: {service_name: cron}
    else:
      - id: e
        type: check_service
        params: {service_name: cron}
"""))
        self.assertEqual(
            [a["branch"] for a in spec["actions"]],
            [None, "b1.then.b2.then", "b1.then.b2.else", "b1.else"])


class BranchDeployTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="t", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="h1", agent_token="tok-t1", status=Host.Status.ONLINE,
            mode=Host.Mode.FULL_CONTROL)

    def _deploy(self, yaml_source):
        d = TaskDefinition.objects.create(
            owner=self.user, name="Branches", yaml_source=yaml_source,
            parsed_spec=parse_and_validate(yaml_source))
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{d.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456"},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return Task.objects.filter(host=self.host).order_by("-created_at").first()

    def test_deploy_signs_flow_and_all_leaves(self):
        task = self._deploy(SPEC_YAML)
        spec = parse_and_validate(SPEC_YAML)
        self.assertEqual(task.params["flow"], spec["flow"])
        self.assertEqual(
            [s["id"] for s in task.params["steps"]],
            ["svc", "stop", "upd", "upd2"])
        self.assertEqual(len(task.params["steps"]), 4)


class BranchFeatureGateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="web-01", agent_token="tok-" + "g" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )

    def _checkin(self, **extra):
        return self.client.post(
            _CHECKIN, {"hostname": self.host.hostname, **extra}, format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )

    def test_branch_task_refused_without_feature(self):
        spec = parse_and_validate(SPEC_YAML)
        import secrets
        task = Task.objects.create(
            host=self.host, action="_script",
            params={"steps": [{"id": a["id"], "action": a["type"],
                               "params": a["params"]}
                              for a in spec["actions"]],
                    "variables": {},
                    "flow": spec["flow"]},
            state=Task.State.PENDING, nonce=secrets.token_hex(32),
        )
        resp = self._checkin(features=["relevant"])  # but no "branches"
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tasks"], [])
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.FAILED)
        self.assertIn("does not understand", task.result_output)
        self.assertIn("if/then/else branches", task.result_output)
        self.assertIsNotNone(task.completed_at)

    def test_branch_task_allowed_with_feature(self):
        spec = parse_and_validate(SPEC_YAML)
        import secrets
        task = Task.objects.create(
            host=self.host, action="_script",
            params={"steps": [{"id": a["id"], "action": a["type"],
                               "params": a["params"]}
                              for a in spec["actions"]],
                    "variables": {},
                    "flow": spec["flow"]},
            state=Task.State.PENDING, nonce=secrets.token_hex(32),
        )
        resp = self._checkin(features=["relevant", "branches"])
        self.assertEqual([t["id"] for t in resp.json()["tasks"]], [str(task.id)])

    def test_plain_task_unaffected(self):
        import secrets
        task = Task.objects.create(
            host=self.host, action="_script",
            params={"steps": [{"id": "a", "action": "check_service",
                               "params": {"service_name": "cron"}}],
                    "variables": {}},
            state=Task.State.PENDING, nonce=secrets.token_hex(32),
        )
        resp = self._checkin()  # old agent: no features at all
        self.assertEqual([t["id"] for t in resp.json()["tasks"]], [str(task.id)])
