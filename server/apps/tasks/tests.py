import base64
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from nacl.signing import VerifyKey
from rest_framework.test import APIClient

from apps.accounts.models import UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.agent_dist.models import AgentBinary
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from apps.tasks.spec import SpecError, parse_and_validate
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
