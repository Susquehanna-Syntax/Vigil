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

    def test_hashes_bundled_exe_file(self):
        # CI keeps PyInstaller's .exe suffix on the Windows artifact — the
        # resolver must find vigil-agent-windows-amd64.exe for that platform.
        content = b"fake-windows-agent-binary"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "vigil-agent-windows-amd64.exe"), "wb") as fh:
                fh.write(content)
            with override_settings(VIGIL_AGENT_DIST_DIR=d):
                self.assertEqual(
                    binary_sha256("windows-amd64"),
                    hashlib.sha256(content).hexdigest(),
                )

    def test_all_binary_sha256_collects_only_available(self):
        AgentBinary.objects.create(platform="linux-amd64", version="1", sha256="c" * 64)
        AgentBinary.objects.create(platform="darwin-arm64", version="1", sha256="d" * 64)
        digests = all_binary_sha256()
        self.assertEqual(digests["linux-amd64"], "c" * 64)
        self.assertEqual(digests["darwin-arm64"], "d" * 64)
        self.assertNotIn("windows-amd64", digests)


@override_settings(VIGIL_AGENT_DIST_DIR="/nonexistent-vigil-test-dist")
class DownloadAgentTests(TestCase):
    def test_downloads_bundled_exe_for_windows(self):
        content = b"fake-windows-agent-binary"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "vigil-agent-windows-amd64.exe"), "wb") as fh:
                fh.write(content)
            with override_settings(VIGIL_AGENT_DIST_DIR=d):
                resp = self.client.get("/agent/download/windows-amd64/")
                self.assertEqual(resp.status_code, 200)
                self.assertIn(
                    'filename="vigil-agent-windows-amd64.exe"',
                    resp["Content-Disposition"],
                )
                self.assertEqual(b"".join(resp.streaming_content), content)

    def test_downloads_bundled_linux_arm64(self):
        content = b"fake-arm64-agent-binary"
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "vigil-agent-linux-arm64"), "wb") as fh:
                fh.write(content)
            with override_settings(VIGIL_AGENT_DIST_DIR=d):
                resp = self.client.get("/agent/download/linux-arm64/")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(b"".join(resp.streaming_content), content)

    def test_404_when_platform_missing(self):
        resp = self.client.get("/agent/download/linux-arm64/")
        self.assertEqual(resp.status_code, 404)
