"""Signing-key rotation, the agent's half (sprint M3 phase 3).

The agent holds a next key only when the key it trusts signed the statement
introducing it, moves to it when the server starts presenting it, and never
takes a key it moved away from back. Files planted in data_dir prove nothing:
every link is re-verified from the root on each load.
"""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey

from vigil_agent import verify


def b64(key: SigningKey) -> str:
    return base64.b64encode(bytes(key.verify_key)).decode()


def statement(signer: SigningKey, nxt: SigningKey) -> dict:
    sig = signer.sign(verify.rotation_payload(b64(nxt))).signature
    return {"next": b64(nxt), "sig": base64.b64encode(sig).decode()}


def sig(key: SigningKey, msg: bytes = b"task") -> bytes:
    return key.sign(msg).signature


class _Agent(unittest.TestCase):
    configured = ""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.old, self.new, self.stranger = SigningKey.generate(), SigningKey.generate(), SigningKey.generate()
        if self.configured == "old":
            self.configured = b64(self.old)
        verify.pin_public_key(self.data, b64(self.old), self.configured)

    def ring(self):
        return verify.get_pinned_key(self.data, self.configured)


class RotationTests(_Agent):
    def test_the_payload_matches_the_servers_byte_for_byte(self):
        self.assertEqual(verify.rotation_payload("K"), b'{"next": "K", "type": "key-rotation", "v": 1}')

    def test_a_next_key_signed_by_the_trusted_one_is_held_and_acknowledged(self):
        self.assertEqual(verify.learn_rotation(self.data, statement(self.old, self.new), self.configured),
                         b64(self.new))
        self.assertEqual(verify.pending_next_key(self.data, self.configured), b64(self.new))
        ring = self.ring()
        ring.verify(b"task", sig(self.old))
        ring.verify(b"task", sig(self.new))          # either signs during the rotation
        with self.assertRaises(BadSignatureError):
            ring.verify(b"task", sig(self.stranger))

    def test_a_statement_not_signed_by_the_trusted_key_is_ignored(self):
        for bad in (statement(self.new, self.new), statement(self.stranger, self.new),
                    {"next": b64(self.new), "sig": "!!"}, {"next": "short", "sig": ""}, "junk", None):
            self.assertIsNone(verify.learn_rotation(self.data, bad, self.configured), bad)
        self.assertIsNone(verify.pending_next_key(self.data, self.configured))

    def test_the_switch_moves_trust_and_drops_the_old_key(self):
        verify.learn_rotation(self.data, statement(self.old, self.new), self.configured)
        ring = verify.pin_public_key(self.data, b64(self.new), self.configured)
        self.assertEqual(ring.trusted_b64, b64(self.new))
        ring.verify(b"task", sig(self.new))
        with self.assertRaises(BadSignatureError):
            ring.verify(b"task", sig(self.old))
        self.assertIsNone(verify.pending_next_key(self.data, self.configured))
        # ...and it lasts: a restart walks the chain to the same place.
        self.assertEqual(self.ring().trusted_b64, b64(self.new))
        verify.pin_public_key(self.data, b64(self.new), self.configured)
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, b64(self.old), self.configured)

    def test_an_unintroduced_key_is_still_refused(self):
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, b64(self.new), self.configured)

    def test_a_retired_key_is_never_taken_back(self):
        verify.learn_rotation(self.data, statement(self.old, self.new), self.configured)
        verify.pin_public_key(self.data, b64(self.new), self.configured)
        self.assertIsNone(verify.learn_rotation(self.data, statement(self.new, self.old), self.configured))
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, b64(self.old), self.configured)

    def test_two_rotations_chain(self):
        third = SigningKey.generate()
        verify.learn_rotation(self.data, statement(self.old, self.new), self.configured)
        verify.pin_public_key(self.data, b64(self.new), self.configured)
        verify.learn_rotation(self.data, statement(self.new, third), self.configured)
        verify.pin_public_key(self.data, b64(third), self.configured)
        self.assertEqual(self.ring().trusted_b64, b64(third))


class TamperTests(_Agent):
    def test_a_planted_chain_link_proves_nothing(self):
        (self.data / "server_public_key.chain").write_text(json.dumps([statement(self.stranger, self.new)]))
        self.assertEqual(self.ring().trusted_b64, b64(self.old))
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, b64(self.new), self.configured)

    def test_a_planted_next_file_proves_nothing(self):
        (self.data / "server_public_key.next").write_text(json.dumps(statement(self.stranger, self.new)))
        self.assertIsNone(verify.pending_next_key(self.data, self.configured))
        with self.assertRaises(BadSignatureError):
            self.ring().verify(b"task", sig(self.new))
        with self.assertRaises(verify.KeyMismatchError):
            verify.pin_public_key(self.data, b64(self.new), self.configured)

    def test_even_a_validly_signed_offer_of_a_retired_key_is_not_held(self):
        # Defence in depth: learn_rotation refuses this, and so does loading.
        verify.learn_rotation(self.data, statement(self.old, self.new), self.configured)
        verify.pin_public_key(self.data, b64(self.new), self.configured)
        (self.data / "server_public_key.next").write_text(json.dumps(statement(self.new, self.old)))
        self.assertIsNone(verify.pending_next_key(self.data, self.configured))
        with self.assertRaises(BadSignatureError):
            self.ring().verify(b"task", sig(self.old))

    def test_garbage_files_do_not_break_the_agent(self):
        (self.data / "server_public_key.chain").write_text("{not json")
        (self.data / "server_public_key.next").write_text("[]")
        self.assertEqual(self.ring().trusted_b64, b64(self.old))
        self.assertIsNone(verify.pending_next_key(self.data, self.configured))


class ConfiguredRootTests(RotationTests):
    """The same, for an agent installed with server_public_key in agent.yml:
    the configured key is the root, the pin file plays no part."""
    configured = "old"

    def test_a_planted_pin_file_still_cannot_override(self):
        (self.data / "server_public_key.pin").write_text(b64(self.stranger))
        self.assertEqual(self.ring().trusted_b64, b64(self.old))


class CheckinAckTests(_Agent):
    def test_the_checkin_acknowledges_the_held_key(self):
        from vigil_agent import client
        config = mock.MagicMock(data_dir=self.data, server_public_key=self.configured, tags=[],
                                allowlist=[], mode="managed", server_url="http://x", agent_token="t")
        sent = {}

        def post(url, json=None, **kw):
            sent.update(json)
            return mock.MagicMock(json=lambda: {}, status_code=200)

        with mock.patch.object(client.requests, "post", side_effect=post), \
                mock.patch.object(client, "_system_info", return_value={}):
            client.checkin(config, [])
            self.assertNotIn("key_ack", sent)
            verify.learn_rotation(self.data, statement(self.old, self.new), self.configured)
            client.checkin(config, [])
        self.assertEqual(sent["key_ack"], b64(self.new))


if __name__ == "__main__":
    unittest.main()
