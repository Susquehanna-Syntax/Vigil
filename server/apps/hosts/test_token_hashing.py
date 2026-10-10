"""Agent tokens are stored as a SHA-256 (architect review, 2026-10-08): a database
dump must not hand out a working credential for every machine."""
import hashlib

from django.test import TestCase

from apps.hosts.models import Host, hash_agent_token

TOKEN = "plain-token-0123456789abcdef"


class TokenHashingTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token=TOKEN, status=Host.Status.ONLINE)

    def test_the_token_is_never_stored_in_the_clear(self):
        self.host.refresh_from_db()
        self.assertNotIn(TOKEN, self.host.agent_token)
        self.assertEqual(self.host.agent_token, hash_agent_token(TOKEN))

    def test_the_plaintext_still_authenticates(self):
        resp = self.client.post("/api/v1/checkin", {"hostname": "h", "metrics": {}},
                                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_the_stored_hash_is_not_a_token(self):
        self.host.refresh_from_db()
        resp = self.client.post("/api/v1/checkin", {"hostname": "h", "metrics": {}},
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}")
        self.assertEqual(resp.status_code, 401)

    def test_registration_refuses_a_hash_shaped_token(self):
        resp = self.client.post("/api/v1/register", {"agent_token": "sha256$" + "a" * 64, "hostname": "x"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_fingerprint_is_what_the_agent_computes(self):
        self.host.refresh_from_db()
        self.assertEqual(self.host.token_fingerprint, hashlib.sha256(TOKEN.encode()).hexdigest())

    def test_saving_twice_does_not_hash_twice(self):
        self.host.refresh_from_db()
        stored = self.host.agent_token
        self.host.save()
        self.host.refresh_from_db()
        self.assertEqual(self.host.agent_token, stored)
