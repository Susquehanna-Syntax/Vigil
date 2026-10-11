"""Images, volumes and networks (14.3): the check-in lists, the container
edges, disk usage, and remove/prune — against a fake engine."""
import tests._safety_net  # noqa: F401
import json
import unittest
from unittest.mock import MagicMock, patch

from vigil_agent import collector, executor
from vigil_agent.actions import docker_resources as dr

DF = {
    "LayersSize": 5000,
    "Images": [{"Id": "sha256:aaa", "Size": 3000, "SharedSize": 1000, "Containers": 0},
               {"Id": "sha256:bbb", "Size": 2000, "SharedSize": 0, "Containers": 1}],
    "Containers": [{"SizeRw": 10}, {"SizeRw": 5}],
    "Volumes": [{"Name": "db", "UsageData": {"Size": 700, "RefCount": 1}},
                {"Name": "old", "UsageData": {"Size": 300, "RefCount": 0}},
                {"Name": "unmeasured", "UsageData": {"Size": -1, "RefCount": -1}}],
    "BuildCache": [{"Size": 40, "InUse": False}, {"Size": 60, "InUse": True}],
}


class DiskUsageTests(unittest.TestCase):
    def test_totals_and_reclaimable(self):
        eng = MagicMock(get=MagicMock(return_value=DF))
        with patch.object(executor, "_engine", return_value=eng):
            out = dr._docker_disk_usage({}, None)
        data = json.loads(out)
        self.assertEqual(data["kind"], "docker_disk_usage")
        self.assertEqual(data["images_reclaimable"], 2000, "unused image minus what it shares")
        self.assertEqual(data["volumes_size"], 1000)
        self.assertEqual(data["volumes_reclaimable"], 300)
        self.assertEqual(data["build_cache_reclaimable"], 40)
        self.assertEqual([v["size"] for v in data["volumes"]], [700, 300, None], "-1 means not measured")
        self.assertEqual(out.data["reclaimable"], 2340)


class RemovePruneTests(unittest.TestCase):
    def _run(self, fn, params, reply=None):
        eng = MagicMock(delete=MagicMock(return_value=reply), post=MagicMock(return_value=reply))
        with patch.object(executor, "_engine", return_value=eng), \
                patch.object(collector, "request_docker_recheck") as recheck:
            out = fn(params, None)
        recheck.assert_called_once()
        return eng, out

    def test_image_remove_never_forces(self):
        eng, out = self._run(dr._image_remove, {"image": "nginx:1.25"}, [{"Untagged": "nginx:1.25"}])
        eng.delete.assert_called_once_with("/images/nginx:1.25")
        self.assertTrue(out.data["removed"])

    def test_image_prune_dangling_by_default_unused_with_all(self):
        reply = {"ImagesDeleted": [{"Deleted": "sha256:x"}], "SpaceReclaimed": 99}
        eng, out = self._run(dr._image_prune, {}, reply)
        self.assertEqual(json.loads(eng.post.call_args.kwargs["query"]["filters"]), {"dangling": ["true"]})
        self.assertEqual((out.data["removed"], out.data["space_reclaimed"]), (1, 99))
        eng, _ = self._run(dr._image_prune, {"all": True}, reply)
        self.assertEqual(json.loads(eng.post.call_args.kwargs["query"]["filters"]), {"dangling": ["false"]})

    def test_volume_prune_all_includes_named(self):
        eng, out = self._run(dr._volume_prune, {"all": "true"}, {"VolumesDeleted": ["old"], "SpaceReclaimed": 300})
        self.assertEqual(json.loads(eng.post.call_args.kwargs["query"]["filters"]), {"all": ["true"]})
        self.assertEqual(out.data["space_reclaimed"], 300)

    def test_names_are_validated_and_builtin_networks_refused(self):
        for fn, params in ((dr._volume_remove, {"volume": "../etc"}), (dr._network_remove, {"network": "a b"}),
                           (dr._image_remove, {"image": "x;rm"}), (dr._network_remove, {"network": "bridge"}),
                           (dr._image_prune, {"all": "yes please"})):
            with self.assertRaises(ValueError, msg=(fn.__name__, params)):
                with patch.object(executor, "_engine", return_value=MagicMock()):
                    fn(params, None)

    def test_registered(self):
        from vigil_agent.config import _ALL_ACTIONS
        for name in ("docker_disk_usage", "image_remove", "image_prune", "volume_remove", "volume_prune",
                     "network_remove", "network_prune"):
            self.assertIn(name, executor._HANDLERS)
            self.assertIn(name, _ALL_ACTIONS)


class CheckinCollectionTests(unittest.TestCase):
    def test_resources_and_container_edges(self):
        replies = {
            "/images/json": [{"Id": "sha256:aaa", "RepoTags": ["nginx:1.25", "<none>:<none>"], "Size": 10, "Created": 5}],
            "/volumes": {"Volumes": [{"Name": "db", "Driver": "local", "Labels": {"com.docker.compose.project": "shop"}}]},
            "/networks": [{"Id": "n1", "Name": "shop_default", "Driver": "bridge", "Scope": "local",
                           "IPAM": {"Config": [{"Subnet": "172.18.0.0/16"}]}}],
        }
        with patch.object(collector, "_engine_available", return_value=True), \
                patch.object(collector, "_docker_api_get", side_effect=lambda p: replies[p]):
            res = collector.collect_docker_resources()
        self.assertEqual(res["images"][0]["tags"], ["nginx:1.25"])
        self.assertEqual(res["volumes"][0]["stack"], "shop")
        self.assertEqual(res["networks"][0]["subnets"], ["172.18.0.0/16"])
        c = {"NetworkSettings": {"Networks": {"shop_default": {"IPAddress": "172.18.0.2", "NetworkID": "n1"}}},
             "Mounts": [{"Type": "volume", "Name": "db", "Source": "/var/lib/docker/volumes/db/_data",
                         "Destination": "/var/lib/mysql", "RW": True}]}
        self.assertEqual(collector._container_networks(c),
                         [{"name": "shop_default", "ip": "172.18.0.2", "network_id": "n1"}])
        self.assertEqual(collector._container_mounts(c)[0]["name"], "db")

    def test_no_engine_sends_nothing(self):
        with patch.object(collector, "_engine_available", return_value=False):
            self.assertIsNone(collector.collect_docker_resources())


if __name__ == "__main__":
    unittest.main()
