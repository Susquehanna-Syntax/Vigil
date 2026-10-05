"""M11 04: the container snapshot comes from the engine client, with what the
stack inventory needs — compose labels, restart policy, the image digest."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import collector, engine

LABELS = {"com.docker.compose.project": "media",
          "com.docker.compose.service": "jellyfin",
          "com.docker.compose.project.config_files": "/opt/media/compose.yaml",
          "com.docker.compose.project.working_dir": "/opt/media",
          "com.docker.compose.config-hash": "abc123"}


class SnapshotTests(unittest.TestCase):
    def test_fields_for_the_stack_inventory(self):
        fake = FakeEngine(routes={
            ("GET", "/containers/json"): (200, [{"Id": "c1", "Names": ["/jellyfin"], "Image": "jellyfin/jellyfin:latest",
                                                 "ImageID": "sha256:img1", "State": "exited", "Status": "Exited (0)",
                                                 "Labels": LABELS, "Ports": []}]),
            ("GET", "/containers/c1/json"): (200, {"HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}),
            ("GET", "/images/sha256:img1/json"): (200, {"RepoDigests": ["jellyfin/jellyfin@sha256:d1"]}),
        })
        self.addCleanup(fake.close)
        with patch.object(engine, "default_client", return_value=engine.EngineClient(fake.path)):
            [row] = collector.collect_docker_containers()
        self.assertEqual((row["stack"], row["service"]), ("media", "jellyfin"))
        self.assertEqual((row["config_files"], row["working_dir"], row["config_hash"]),
                         ("/opt/media/compose.yaml", "/opt/media", "abc123"))
        self.assertEqual((row["restart_policy"], row["image_digest"]),
                         ("unless-stopped", "jellyfin/jellyfin@sha256:d1"))

    def test_no_engine_means_no_snapshot(self):
        with patch.object(engine, "default_client", return_value=None):
            self.assertIsNone(collector.collect_docker_containers())


if __name__ == "__main__":
    unittest.main()
