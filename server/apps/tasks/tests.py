import base64
import json
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from nacl.signing import VerifyKey
from rest_framework.test import APIClient

from apps.accounts.models import UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.agent_dist.models import AgentBinary
from apps.hosts.models import Host
from apps.tasks.expression import ExprError, evaluate, parse, validate
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import SpecError, parse_and_validate
from apps.tasks.tasks import expire_stale_tasks
from vigil.signing import get_public_key_b64, sign_task

UPDATE_AGENT_YAML = "name: Update agent\nrisk: standard\nactions:\n  - type: update_agent\n"


class SpecValidationTests(TestCase):
    def test_accepts_valid_definition(self):
        spec = parse_and_validate(UPDATE_AGENT_YAML)
        self.assertEqual(spec["name"], "Update agent")
        self.assertEqual(len(spec["actions"]), 1)
        self.assertEqual(spec["actions"][0]["type"], "update_agent")

    def test_rejects_unknown_action(self):
        with self.assertRaises(SpecError):
            parse_and_validate("name: Bad\nactions:\n  - type: not_a_real_action\n")

    def test_rejects_empty_yaml(self):
        with self.assertRaises(SpecError):
            parse_and_validate("")

    def test_pull_and_recreate_template_validates_at_standard_risk(self):
        # Shape emitted by the alerts panel's Suggest Fix button.
        yaml_src = (
            "name: 'Update Docker Image: nginx:latest'\n"
            "actions:\n"
            "  - id: pull_new_image\n"
            "    type: pull_image\n"
            "    params: { image: 'nginx:latest' }\n"
            "  - id: recreate\n"
            "    type: recreate_container\n"
            "    params: { container_name: nginx, image: 'nginx:latest' }\n"
        )
        spec = parse_and_validate(yaml_src)
        self.assertEqual([a["type"] for a in spec["actions"]], ["pull_image", "recreate_container"])
        self.assertEqual(spec["risk"], "standard")

    def test_recreate_container_requires_container_name(self):
        with self.assertRaises(SpecError):
            parse_and_validate(
                "name: Bad\nactions:\n"
                "  - type: recreate_container\n"
                "    params: { image: 'nginx:latest' }\n"
            )


class SigningTests(TestCase):
    def test_sign_task_signature_verifies(self):
        host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )
        task = Task.objects.create(
            host=host, action="_script", params={"steps": []},
            nonce="n" * 32, ttl_seconds=300,
        )
        signature = sign_task(task)

        payload = json.dumps(
            {
                "id": str(task.id),
                "host_id": str(task.host_id),
                "action": task.action,
                "params": task.params,
                "nonce": task.nonce,
                "ttl_seconds": task.ttl_seconds,
            },
            sort_keys=True,
        ).encode()

        verify_key = VerifyKey(base64.b64decode(get_public_key_b64()))
        # Raises nacl.exceptions.BadSignatureError if the signature is invalid.
        verify_key.verify(payload, base64.b64decode(signature))


@override_settings(VIGIL_AGENT_DIST_DIR="/nonexistent-vigil-test-dist")
class UpdateAgentDeployTests(TestCase):
    """The deploy path must stamp a verified SHA-256 into update_agent tasks."""

    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        profile = UserProfile.objects.create(user=self.user)
        self.secret = generate_secret()
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.user)

        self.host = Host.objects.create(
            hostname="h1", agent_token="z" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )
        AgentBinary.objects.create(platform="linux-amd64", version="1.0", sha256="a" * 64)

        self.definition = TaskDefinition.objects.create(
            owner=self.user,
            name="Update agent",
            yaml_source=UPDATE_AGENT_YAML,
            parsed_spec=parse_and_validate(UPDATE_AGENT_YAML),
        )

    def test_deploy_stamps_binary_sha256(self):
        resp = self.client.post(
            f"/api/v1/tasks/definitions/{self.definition.id}/deploy/",
            {"host_ids": [str(self.host.id)], "totp": generate_totp(self.secret)},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))

        task = Task.objects.get(host=self.host)
        step = task.params["steps"][0]
        self.assertEqual(step["action"], "update_agent")
        self.assertEqual(step["params"]["binary_sha256"]["linux-amd64"], "a" * 64)

    def test_deploy_rejected_without_totp(self):
        resp = self.client.post(
            f"/api/v1/tasks/definitions/{self.definition.id}/deploy/",
            {"host_ids": [str(self.host.id)]},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(Task.objects.filter(host=self.host).exists())


class ExpressionEvaluatorTests(TestCase):
    """The ``when:`` predicate grammar — safety and evaluation."""

    CTX = {"agent": {"os": "linux", "pkg_manager": "apt"}, "inputs": {}, "host": {}}

    def test_basic_comparisons(self):
        self.assertTrue(evaluate('agent.os == "linux"', self.CTX))
        self.assertFalse(evaluate('agent.os == "windows"', self.CTX))
        self.assertTrue(evaluate('agent.pkg_manager in ("apt", "dnf")', self.CTX))
        self.assertTrue(
            evaluate('agent.os == "linux" and agent.pkg_manager == "apt"', self.CTX)
        )

    def test_missing_key_is_none_not_error(self):
        self.assertFalse(evaluate('inputs.nope == "x"', self.CTX))
        self.assertTrue(evaluate("inputs.nope == None", self.CTX))

    def test_membership_against_missing_is_typeerror_safe(self):
        # `None in "linux"` / `x in 5` would raise TypeError in Python;
        # the evaluator must resolve them to False / True, never crash.
        self.assertFalse(evaluate("inputs.missing in agent.os", self.CTX))
        self.assertTrue(evaluate("inputs.missing not in agent.os", self.CTX))

    def test_rejects_function_calls(self):
        with self.assertRaises(ExprError):
            parse('len(agent.os) == 5')

    def test_rejects_dunder_attribute(self):
        with self.assertRaises(ExprError):
            parse('agent.__class__ == "x"')

    def test_rejects_deep_attribute_chain(self):
        with self.assertRaises(ExprError):
            parse("agent.a.b.c == 1")

    def test_rejects_unknown_root(self):
        with self.assertRaises(ExprError):
            parse('server.secret == "x"')

    def test_validate_is_syntax_only(self):
        # validate() doesn't need a context — it only checks the grammar.
        validate('agent.os == "linux"')
        with self.assertRaises(ExprError):
            validate("agent.os ==")


class ExpressionCopySyncTests(TestCase):
    """The server and agent ship byte-identical copies of expression.py.

    They are imported by both sides and the security of ``when:`` rests
    on them staying in lockstep — a drift is a real bug, so assert it.
    """

    def test_server_and_agent_copies_identical(self):
        server_copy = Path(__file__).resolve().parent / "expression.py"
        agent_copy = (
            Path(__file__).resolve().parents[3]
            / "agent" / "vigil_agent" / "expression.py"
        )
        if not agent_copy.exists():
            self.skipTest("agent copy not present in this checkout")
        self.assertEqual(
            server_copy.read_bytes(),
            agent_copy.read_bytes(),
            "server and agent expression.py have drifted — re-sync them",
        )


class ExpireStaleTasksTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )

    def _task(self, *, dispatched_ago_seconds, ttl=300, **kw):
        t = Task.objects.create(
            host=self.host, action="_script", params={"steps": []},
            nonce=("n" * 31) + str(Task.objects.count() % 10),
            ttl_seconds=ttl, state=Task.State.DISPATCHED,
            **kw,
        )
        Task.objects.filter(pk=t.pk).update(
            dispatched_at=now() - timedelta(seconds=dispatched_ago_seconds)
        )
        return t

    @override_settings(VIGIL_TASK_EXPIRY_GRACE_SECONDS=3600)
    def test_overdue_dispatched_task_expires(self):
        task = self._task(dispatched_ago_seconds=300 + 3600 + 60)
        expire_stale_tasks()
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.EXPIRED)
        self.assertIsNotNone(task.completed_at)

    @override_settings(VIGIL_TASK_EXPIRY_GRACE_SECONDS=3600)
    def test_recent_dispatched_task_survives(self):
        task = self._task(dispatched_ago_seconds=60)
        expire_stale_tasks()
        task.refresh_from_db()
        self.assertEqual(task.state, Task.State.DISPATCHED)

    @override_settings(VIGIL_TASK_EXPIRY_GRACE_SECONDS=3600)
    def test_expiry_finalizes_run(self):
        run = TaskRun.objects.create(
            name_snapshot="r", host_count=1, step_count=1,
            state=TaskRun.State.RUNNING,
        )
        self._task(dispatched_ago_seconds=300 + 3600 + 60, run=run, step_order=0)
        expire_stale_tasks()
        run.refresh_from_db()
        self.assertEqual(run.state, TaskRun.State.FAILED)


class TaskHistorySoftDeleteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
        )

    def _task(self, state):
        return Task.objects.create(
            host=self.host, action="_script", params={"steps": []},
            nonce=("n" * 31) + str(Task.objects.count() % 10),
            state=state,
        )

    def test_delete_hides_but_preserves_row(self):
        task = self._task(Task.State.COMPLETED)
        resp = self.client.delete(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 204)
        task.refresh_from_db()  # row still exists — audit trail is immutable
        self.assertTrue(task.hidden)

    def test_hidden_task_absent_from_history(self):
        task = self._task(Task.State.COMPLETED)
        self.client.delete(f"/api/v1/tasks/{task.id}/")
        resp = self.client.get("/api/v1/tasks/history/")
        ids = [t["id"] for t in resp.data["results"]]
        self.assertNotIn(str(task.id), ids)

    def test_in_flight_task_cannot_be_deleted(self):
        task = self._task(Task.State.DISPATCHED)
        resp = self.client.delete(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 409)
        task.refresh_from_db()
        self.assertFalse(task.hidden)

    def test_history_does_not_expose_nonce(self):
        self._task(Task.State.COMPLETED)
        resp = self.client.get("/api/v1/tasks/history/")
        self.assertTrue(resp.data["results"])
        self.assertNotIn("nonce", resp.data["results"][0])


class CommunityTemplatesTests(TestCase):
    """The Community tab is backed by the public GitHub repo via a cached proxy."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)

    def _mock_response(self, status_code=200, json_data=None, text=""):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data
        resp.text = text
        resp.raise_for_status.side_effect = (
            None if status_code < 400 else Exception(f"HTTP {status_code}")
        )
        return resp

    def test_empty_repo_returns_empty_list(self):
        from unittest.mock import patch
        with patch("requests.get", return_value=self._mock_response(404)):
            resp = self.client.get("/api/v1/tasks/community/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_parses_valid_yaml_and_skips_invalid(self):
        from unittest.mock import patch
        listing = self._mock_response(200, json_data=[
            {"type": "file", "name": "good.yaml", "download_url": "https://x/good.yaml",
             "html_url": "https://github.com/x/good.yaml"},
            {"type": "file", "name": "bad.yaml", "download_url": "https://x/bad.yaml",
             "html_url": "https://github.com/x/bad.yaml"},
            {"type": "dir", "name": "sub", "download_url": None},
            {"type": "file", "name": "readme.md", "download_url": "https://x/readme.md"},
        ])
        good = self._mock_response(200, text=UPDATE_AGENT_YAML)
        bad = self._mock_response(200, text="not: [valid task yaml")

        def fake_get(url, **kwargs):
            if "good.yaml" in url:
                return good
            if "bad.yaml" in url:
                return bad
            return listing

        with patch("requests.get", side_effect=fake_get):
            resp = self.client.get("/api/v1/tasks/community/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        entry = resp.data[0]
        self.assertEqual(entry["filename"], "good.yaml")
        self.assertEqual(entry["name"], "Update agent")
        self.assertEqual(entry["yaml_source"], UPDATE_AGENT_YAML)
        self.assertIn("actions", entry["parsed_spec"])

    def test_result_is_cached(self):
        from unittest.mock import patch
        with patch("requests.get", return_value=self._mock_response(404)) as mocked:
            self.client.get("/api/v1/tasks/community/")
            self.client.get("/api/v1/tasks/community/")
        self.assertEqual(mocked.call_count, 1)

    def test_unreachable_repo_returns_502(self):
        from unittest.mock import patch
        with patch("requests.get", side_effect=OSError("no network")):
            resp = self.client.get("/api/v1/tasks/community/")
        self.assertEqual(resp.status_code, 502)
