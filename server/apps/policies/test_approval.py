"""Approve mode: runs propose, people approve (and dispatch) or reject."""
from unittest import mock

from django.test import TestCase

from apps.tasks.models import Task

from .models import PolicyChange
from .run import run_policy
from .test_drift import give, make_host
from .test_policy_api import admin_client
from .test_run import _TOTP, make_policy


class ApprovalTests(TestCase):
    def setUp(self):
        self.client, self.user = admin_client()
        self.stale = make_host("a-stale")
        give(self.stale, "curl", "8.4", "8.5")
        fine = make_host("b-fine")
        give(fine, "curl", "8.5", "8.5")
        self.policy = make_policy(approval_mode="approve")

    def test_a_run_queues_instead_of_dispatching(self):
        result = run_policy(self.policy, user=self.user)
        self.assertEqual((result["mode"], result["queued"]), ("approval", 1))
        self.assertFalse(Task.objects.exists())
        change = PolicyChange.objects.get()
        self.assertEqual(change.host, self.stale)
        self.assertEqual(change.changes[0]["action"], "upgrade")

    def test_a_second_run_refreshes_rather_than_duplicates(self):
        run_policy(self.policy, user=self.user)
        run_policy(self.policy, user=self.user)
        self.assertEqual(PolicyChange.objects.filter(state="pending").count(), 1)

    def test_approve_dispatches_to_those_hosts(self):
        run_policy(self.policy, user=self.user)
        change = PolicyChange.objects.get()
        listed = self.client.get("/api/v1/policies/changes/?state=pending").json()["results"]
        self.assertEqual([c["id"] for c in listed], [str(change.id)])
        with mock.patch(_TOTP, return_value=None):
            resp = self.client.post("/api/v1/policies/changes/approve/",
                                    {"ids": [str(change.id)], "totp": "1"}, format="json")
        self.assertEqual(resp.json(), {"approved": 1, "dispatched": 1})
        self.assertEqual(Task.objects.get().host, self.stale)
        change.refresh_from_db()
        self.assertEqual((change.state, change.decided_by), ("dispatched", self.user))
        # Approving again does nothing — the row is no longer pending.
        with mock.patch(_TOTP, return_value=None):
            again = self.client.post("/api/v1/policies/changes/approve/",
                                     {"ids": [str(change.id)], "totp": "1"}, format="json")
        self.assertEqual(again.json()["dispatched"], 0)
        self.assertEqual(Task.objects.count(), 1)

    def test_approve_needs_totp(self):
        run_policy(self.policy, user=self.user)
        change = PolicyChange.objects.get()
        with mock.patch(_TOTP, return_value="bad code"):
            resp = self.client.post("/api/v1/policies/changes/approve/",
                                    {"ids": [str(change.id)]}, format="json")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(Task.objects.exists())

    def test_reject(self):
        run_policy(self.policy, user=self.user)
        change = PolicyChange.objects.get()
        resp = self.client.post("/api/v1/policies/changes/reject/",
                                {"ids": [str(change.id)]}, format="json")
        self.assertEqual(resp.json(), {"rejected": 1})
        change.refresh_from_db()
        self.assertEqual(change.state, "rejected")
        self.assertFalse(Task.objects.exists())

    def test_bad_ids_are_refused(self):
        for body in ({}, {"ids": []}, {"ids": "x"}, {"ids": ["not-a-uuid"]}):
            with self.subTest(body=body):
                resp = self.client.post("/api/v1/policies/changes/reject/", body,
                                        format="json")
                self.assertEqual(resp.status_code, 400)
