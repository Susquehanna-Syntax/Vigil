import hashlib
import os
import tempfile

from django.test import TestCase, override_settings

from apps.agent_dist.models import AgentBinary
from apps.agent_dist.views import all_binary_sha256, binary_sha256


@override_settings(VIGIL_AGENT_DIST_DIR="/nonexistent-vigil-test-dist")
class BinarySha256Tests(TestCase):
    def test_hashes_bundled_file(self):
        content = b"fake-bundled-agent-binary"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "vigil-agent-linux-amd64"), "wb") as fh:
                fh.write(content)
            with override_settings(VIGIL_AGENT_DIST_DIR=d):
                self.assertEqual(
                    binary_sha256("linux-amd64"),
                    hashlib.sha256(content).hexdigest(),
                )

    def test_uses_stored_digest_when_no_bundle(self):
        AgentBinary.objects.create(platform="linux-amd64", version="1", sha256="b" * 64)
        self.assertEqual(binary_sha256("linux-amd64"), "b" * 64)

    def test_empty_when_no_binary(self):
        self.assertEqual(binary_sha256("windows-amd64"), "")

    def test_all_binary_sha256_collects_only_available(self):
        AgentBinary.objects.create(platform="linux-amd64", version="1", sha256="c" * 64)
        AgentBinary.objects.create(platform="darwin-arm64", version="1", sha256="d" * 64)
        digests = all_binary_sha256()
        self.assertEqual(digests["linux-amd64"], "c" * 64)
        self.assertEqual(digests["darwin-arm64"], "d" * 64)
        self.assertNotIn("windows-amd64", digests)
