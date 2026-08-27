"""Wave management API — the Deployments › Waves panel's backend.

Reads are open to any authenticated user so the page can render; writes are
admin-only, because a wave's tags decide which machines the next rollout
touches.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host

from .models import PatchRollout, PatchWave, TaskDefinition
from .spec import parse_and_validate

YAML = ("name: Patch\nrisk: low\nactions:\n"
        "  - type: run_package_updates\n    params:\n      security_only: true\n")


class WaveApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("admin1", password="x",
                                              is_staff=True, is_superuser=True)
        self.viewer = User.objects.create_user("viewer1", password="x")
        self.wave = PatchWave.objects.create(
            name="Canary", order=1, tags=["canary"], validation_hours=1)

    def _host(self, name, tags, ip):
        return Host.objects.create(hostname=name, ip_address=ip, tags=tags,
                                   agent_token=f"tok-{name}")

    # ── reads ────────────────────────────────────────────────────────────
    def test_list_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v1/waves/").status_code, 403)

    def test_viewer_can_list(self):
        self.client.force_login(self.viewer)
        r = self.client.get("/api/v1/waves/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([w["name"] for w in r.json()], ["Canary"])

    def test_list_is_ordered(self):
        PatchWave.objects.create(name="Broad", order=2, tags=["broad"])
        PatchWave.objects.create(name="Rest", order=3, tags=["server"])
        self.client.force_login(self.viewer)
        self.assertEqual([w["order"] for w in self.client.get("/api/v1/waves/").json()],
                         [1, 2, 3])

    # ── host counts ──────────────────────────────────────────────────────
    def test_host_counts_distinguish_matching_from_exclusive(self):
        """A host in two waves is patched in the earlier one only, so the later
        wave's *matching* count overstates what it will touch."""
        PatchWave.objects.create(name="Broad", order=2, tags=["server"])
        self._host("a", ["canary", "server"], "10.0.0.1")
        self._host("b", ["server"], "10.0.0.2")
        self.client.force_login(self.viewer)
        waves = {w["name"]: w for w in self.client.get("/api/v1/waves/").json()}
        # host "a" matches both waves but belongs to Canary.
        self.assertEqual(waves["Broad"]["host_count"], 2)
        self.assertEqual(waves["Broad"]["exclusive_host_count"], 1)
        self.assertEqual(waves["Canary"]["exclusive_host_count"], 1)

    # ── writes ───────────────────────────────────────────────────────────
    def test_viewer_cannot_create(self):
        self.client.force_login(self.viewer)
        r = self.client.post("/api/v1/waves/",
                             {"name": "X", "order": 9, "tags": ["x"]},
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_admin_creates(self):
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/waves/",
                             {"name": "Broad", "order": 2, "tags": ["broad"],
                              "validation_hours": 12},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(PatchWave.objects.filter(name="Broad").exists())

    def test_duplicate_order_is_rejected_with_a_readable_message(self):
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/waves/",
                             {"name": "Clash", "order": 1, "tags": ["x"]},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("order", r.json())

    def test_wave_without_tags_is_rejected(self):
        """A tagless wave silently matches nothing — refuse it at the edge."""
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/waves/",
                             {"name": "Empty", "order": 5, "tags": []},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("tags", r.json())

    def test_blank_tags_are_stripped_not_kept(self):
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/waves/",
                             {"name": "Trimmed", "order": 6,
                              "tags": ["  keep  ", "", "   "]},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["tags"], ["keep"])

    def test_absurd_validation_window_is_rejected(self):
        self.client.force_login(self.admin)
        r = self.client.post("/api/v1/waves/",
                             {"name": "Slow", "order": 7, "tags": ["x"],
                              "validation_hours": 100000},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_admin_edits(self):
        self.client.force_login(self.admin)
        r = self.client.patch(f"/api/v1/waves/{self.wave.id}/",
                              {"validation_hours": 48},
                              content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.wave.refresh_from_db()
        self.assertEqual(self.wave.validation_hours, 48)

    def test_viewer_cannot_edit(self):
        self.client.force_login(self.viewer)
        r = self.client.patch(f"/api/v1/waves/{self.wave.id}/",
                              {"validation_hours": 48},
                              content_type="application/json")
        self.assertEqual(r.status_code, 403)

    # ── delete ───────────────────────────────────────────────────────────
    def test_admin_deletes(self):
        self.client.force_login(self.admin)
        r = self.client.delete(f"/api/v1/waves/{self.wave.id}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(PatchWave.objects.filter(pk=self.wave.pk).exists())

    def test_delete_refused_while_a_rollout_stands_on_the_wave(self):
        """Deleting it would null current_wave and strand the rollout."""
        definition = TaskDefinition.objects.create(
            name="Patch", owner=self.admin, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")
        PatchRollout.objects.create(
            definition=definition, current_wave=self.wave,
            state=PatchRollout.State.RUNNING)
        self.client.force_login(self.admin)
        r = self.client.delete(f"/api/v1/waves/{self.wave.id}/")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(PatchWave.objects.filter(pk=self.wave.pk).exists())

    def test_delete_allowed_once_the_rollout_completed(self):
        definition = TaskDefinition.objects.create(
            name="Patch2", owner=self.admin, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")
        PatchRollout.objects.create(
            definition=definition, current_wave=self.wave,
            state=PatchRollout.State.COMPLETED)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.delete(f"/api/v1/waves/{self.wave.id}/").status_code, 204)

    def test_unknown_wave_is_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/api/v1/waves/999999/").status_code, 404)
