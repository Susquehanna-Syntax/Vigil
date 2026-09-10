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


class BundledDigestTests(TestCase):
    """The installer refuses a binary the server cannot vouch for, so the path
    every default install takes has to publish a digest.

    Only the manually-uploaded DB record ever did; the bundled build artifact —
    what a real deployment serves — did not, so the one install path in use was
    the one that shipped unverifiable.
    """

    # download_agent is AllowAny by design — agents fetch before they have a
    # token — so these need no login.

    def test_a_bundled_binary_is_served_with_its_sha256(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        payload = b"#!/bin/sh\necho vigil-agent\n"
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "vigil-agent-linux-amd64"
            binary.write_bytes(payload)
            with patch("apps.agent_dist.views._bundled_path", return_value=binary):
                resp = self.client.get("/agent/download/linux-amd64/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Vigil-SHA256"], hashlib.sha256(payload).hexdigest())

    def test_the_digest_tracks_the_bytes(self):
        """A rebuilt image must not serve the previous build's digest."""
        import hashlib
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "vigil-agent-linux-amd64"
            binary.write_bytes(b"first build")
            with patch("apps.agent_dist.views._bundled_path", return_value=binary):
                first = self.client.get("/agent/download/linux-amd64/")["X-Vigil-SHA256"]
                binary.write_bytes(b"second build, different bytes")
                second = self.client.get("/agent/download/linux-amd64/")["X-Vigil-SHA256"]

        self.assertEqual(first, hashlib.sha256(b"first build").hexdigest())
        self.assertEqual(second, hashlib.sha256(b"second build, different bytes").hexdigest())

    def test_the_installer_digest_matches_the_self_updater_digest(self):
        """The installer and the self-updater must check the same number.

        They are different code paths in different languages; the only thing
        making them agree is that both read binary_sha256.
        """
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "vigil-agent-linux-amd64"
            binary.write_bytes(b"the one true build")
            with patch("apps.agent_dist.views._bundled_path", return_value=binary):
                served = self.client.get("/agent/download/linux-amd64/")["X-Vigil-SHA256"]
                stamped = all_binary_sha256().get("linux-amd64")

        self.assertEqual(served, stamped)
