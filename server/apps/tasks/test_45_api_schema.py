"""The rollout and wave endpoints phase 45 typed: each body against the
serializer its ``@extend_schema`` names, with data present so a list's item
shape is checked. Every code here is one an enrolled secret really produces —
the gate under test has no bypass."""

import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from vigil.schema_check import assert_shape

from .api_schema import (
    TasksBatchHaltSerializer,
    TasksDetailSerializer,
    TasksRolloutSerializer,
    TasksRolloutStartResponseSerializer,
    TasksWaveGroupsSerializer,
    TasksWaveHostsSerializer,
    TasksWaveOrderConflictSerializer,
    TasksWaveSerializer,
)
from .models import PatchWave
from .test_rollout import make_definition, make_host, make_wave
from .test_rollout_batch import _definition as batch_definition
from .test_rollout_batch import _host as batch_host
from .test_rollout_batch import _wave as batch_wave

STEP = 30


class RolloutApiSchemaTests(TestCase):
    def setUp(self):
        # One request per TOTP step: a code is burned for its whole validity
        # window once accepted, and the codes below are generated against this
        # frozen clock — the engine's own patched-clock pattern, so no test here
        # races an operator code expiring mid-run.
        self.now = time.time() - STEP
        self.clock = patch("apps.accounts.totp.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

        self.client = APIClient()
        self.operator = get_user_model().objects.create_user("op45", password="pw")
        self.secret = generate_secret()
        profile = UserProfile.objects.create(user=self.operator, role=Role.OPERATOR)
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.operator)

        self.admin = get_user_model().objects.create_user("root45", password="pw",
                                                          is_staff=True,
                                                          is_superuser=True)
        self.admin_secret = generate_secret()
        admin_profile = UserProfile.objects.create(user=self.admin)
        admin_profile.totp_secret = self.admin_secret
        admin_profile.totp_confirmed_at = now()
        admin_profile.save()

        self.definition = make_definition(self.operator)
        self.wave = make_wave("Canary", 1, ["patch:canary"], validation_hours=2)
        self.host = make_host("canary-1", ["patch:canary"])

        batch_definition(self.operator, "Batch OS")
        batch_wave("srv-canary", 1, ["srv45"], groups=["Servers"], validation_hours=0)
        batch_wave("desk-canary", 1, ["desk45"], groups=["Desktops"], validation_hours=0)
        batch_host("srv-45", ["srv45"])
        batch_host("desk-45", ["desk45"])

    def _totp(self, secret=None):
        """A fresh code for a gated request. The clock advances one step per
        call, so no two requests ever hand the gate the same code."""
        self.now += STEP
        return generate_totp(secret or self.secret)

    def ok(self, resp, serializer, status=200):
        self.assertEqual(resp.status_code, status, resp.content)
        assert_shape(self, resp.json(), serializer)

    def test_45_rollout_endpoints_match_the_api_schema(self):
        batch_started = self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id),
             "wave_group_tags": ["Servers", "Desktops"],
             "totp": self._totp()}, format="json")
        self.ok(batch_started, TasksRolloutStartResponseSerializer, 201)
        batch_body = batch_started.json()
        self.assertEqual(len(batch_body["rollouts"]), 2)
        self.assertTrue(batch_body["rollouts"][0]["waves"])
        batch_id = batch_body["batch"]

        started = self.client.post("/api/v1/rollouts/",
                                   {"definition_id": str(self.definition.id),
                                    "totp": self._totp()}, format="json")
        self.ok(started, TasksRolloutStartResponseSerializer, 201)
        body = started.json()
        self.assertTrue(body["waves"], "a wave list needs an item to check")
        rollout_id = body["id"]

        self.ok(self.client.get("/api/v1/rollouts/"), TasksRolloutSerializer(many=True))
        self.ok(self.client.get(f"/api/v1/rollouts/{rollout_id}/"), TasksRolloutSerializer)
        hosts = self.client.get(
            f"/api/v1/rollouts/{rollout_id}/waves/{self.wave.id}/hosts/")
        self.ok(hosts, TasksWaveHostsSerializer)
        self.assertTrue(hosts.json()["hosts"], "a host list needs an item to check")

        self.client.force_authenticate(self.admin)
        # The canary machine has reported nothing, so the rollout is running its
        # first rung: halt it, then resume that halt.
        self.ok(self.client.post(f"/api/v1/rollouts/{rollout_id}/halt/",
                                 {"reason": "schema check",
                                  "totp": self._totp(self.admin_secret)},
                                 format="json"), TasksRolloutSerializer)
        self.ok(self.client.post(f"/api/v1/rollouts/{rollout_id}/resume/",
                                 {"totp": self._totp(self.admin_secret)},
                                 format="json"), TasksRolloutSerializer)
        # Halted, not in a validation window: the skip refuses with the reason.
        self.ok(self.client.post(f"/api/v1/rollouts/{rollout_id}/skip-validation/",
                                 {"totp": self._totp(self.admin_secret)},
                                 format="json"), TasksDetailSerializer, 400)
        self.ok(self.client.post(
            f"/api/v1/rollouts/batch/{batch_id}/halt/",
            {"reason": "schema check",
             "totp": self._totp(self.admin_secret)},
            format="json"), TasksBatchHaltSerializer)

    def test_45_wave_endpoints_match_the_api_schema(self):
        self.ok(self.client.get("/api/v1/wave-groups/"), TasksWaveGroupsSerializer)
        self.ok(self.client.get("/api/v1/waves/"), TasksWaveSerializer(many=True))
        refused = self.client.post(
            "/api/v1/waves/",
            {"name": "Third", "order": 3, "tags": ["role:db"],
             "group_tags": ["Servers"], "validation_hours": 2}, format="json")
        self.assertEqual(refused.status_code, 403, refused.content)

        self.client.force_authenticate(self.admin)
        created = self.client.post(
            "/api/v1/waves/",
            {"name": "Third", "order": 3, "tags": ["role:db"],
             "group_tags": ["Servers"], "validation_hours": 2}, format="json")
        self.ok(created, TasksWaveSerializer, 201)
        wave = PatchWave.objects.get(name="Third")
        self.ok(self.client.get(f"/api/v1/waves/{wave.id}/"), TasksWaveSerializer)
        self.ok(self.client.patch(f"/api/v1/waves/{wave.id}/",
                                  {"validation_hours": 4}, format="json"),
                TasksWaveSerializer)
        self.ok(self.client.post("/api/v1/waves/",
                                 {"name": "Clash", "order": 3, "tags": ["x"],
                                  "group_tags": ["Servers"]}, format="json"),
                TasksWaveOrderConflictSerializer, 400)
