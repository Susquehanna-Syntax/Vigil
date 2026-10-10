"""Security properties of the two unauthenticated routes (§4.2, §4.3)."""
import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host, hash_agent_token
from apps.reprovision import jobs
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob

S = RebuildJob.State


class InstallerEndpointTests(TestCase):
    def setUp(self):
        self.image = OSImage.objects.create(
            name="Ubuntu", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="e" * 64,
            status=OSImage.Status.READY)
        self.profile = InstallProfile.objects.create(
            name="std", image=self.image, disk_target="/dev/sda",
            admin_username="v")
        self.host = Host.objects.create(
            hostname="h1", agent_token="tok-h1", status=Host.Status.ONLINE)
        self.job = RebuildJob.objects.create(
            host=self.host, image=self.image, profile=self.profile,
            state=S.REBOOTING, deadline=now() + timedelta(minutes=60))
        self.answer, self.enroll = jobs.mint_tokens(self.job)
        jobs.stash_enroll_token(self.job, self.enroll)

    # ── Answer file ──────────────────────────────────────────────────────
    def test_answer_served_while_rebooting(self):
        resp = self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertEqual(resp.status_code, 200)

    def test_first_fetch_moves_the_job_to_installing(self):
        # This fetch is the only positive proof the installer booted.
        self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, S.INSTALLING)
        self.assertIsNotNone(self.job.answer_fetched_at)

    def test_unknown_token_404s(self):
        resp = self.client.get("/reprovision/answer/not-a-real-token/user-data")
        self.assertEqual(resp.status_code, 404)

    def test_answer_404s_once_the_job_is_terminal(self):
        self.job.state = S.COMPLETED
        self.job.save(update_fields=["state"])
        resp = self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertEqual(resp.status_code, 404)

    def test_answer_404s_before_the_reboot(self):
        self.job.state = S.STAGED
        self.job.save(update_fields=["state"])
        resp = self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertEqual(resp.status_code, 404)

    def test_repeat_fetch_from_the_same_ip_is_allowed(self):
        # subiquity re-reads its data source; strict single-fetch breaks it.
        url = f"/reprovision/answer/{self.answer}/user-data"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_fetch_from_a_different_ip_is_refused(self):
        url = f"/reprovision/answer/{self.answer}/user-data"
        self.client.get(url, REMOTE_ADDR="10.0.0.5")
        resp = self.client.get(url, REMOTE_ADDR="10.0.0.99")
        self.assertEqual(resp.status_code, 404)

    def test_answer_is_not_cached(self):
        resp = self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertIn("no-store", resp["Cache-Control"])

    def test_meta_data_is_servable_for_subiquity(self):
        resp = self.client.get(f"/reprovision/answer/{self.answer}/meta-data")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("local-hostname", resp.content.decode())

    def test_unknown_filename_404s(self):
        resp = self.client.get(f"/reprovision/answer/{self.answer}/passwd")
        self.assertEqual(resp.status_code, 404)

    def test_answer_carries_the_enrolment_token(self):
        resp = self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertIn(self.enroll, resp.content.decode())

    def test_old_agent_token_is_revoked_at_the_point_of_no_return(self):
        # A token recovered from a backup of the wiped disk must not keep
        # authenticating as this host.
        self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertFalse(Host.objects.by_token("tok-h1").exists())

    # ── Enrolment ────────────────────────────────────────────────────────
    def _installing(self):
        self.job.state = S.INSTALLING
        self.job.save(update_fields=["state"])

    def _enrol(self, **over):
        body = {"enroll_token": self.enroll, "agent_token": "brand-new-token-0001",
                "hostname": "h1"}
        body.update(over)
        return self.client.post("/api/v1/reprovision/enroll", body,
                                content_type="application/json")

    def test_enrol_rotates_the_token_on_the_same_host_row(self):
        self._installing()
        resp = self._enrol()
        self.assertEqual(resp.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.agent_token, hash_agent_token("brand-new-token-0001"))
        self.assertEqual(self.host.status, Host.Status.ONLINE)

    def test_enrol_keeps_the_same_host_id(self):
        self._installing()
        original = self.host.id
        self._enrol()
        self.host.refresh_from_db()
        self.assertEqual(self.host.id, original)

    def test_enrol_token_cannot_be_redeemed_twice(self):
        self._installing()
        self.assertEqual(self._enrol(agent_token="first-token-000001").status_code, 200)
        second = self._enrol(agent_token="second-token-00002")
        self.assertEqual(second.status_code, 403)
        self.host.refresh_from_db()
        self.assertEqual(self.host.agent_token, hash_agent_token("first-token-000001"))

    def test_enrol_rejected_before_installing(self):
        self.assertEqual(self._enrol().status_code, 403)

    def test_enrol_refuses_a_token_that_is_not_token_shaped(self):
        self._installing()
        self.assertEqual(self._enrol(agent_token="sha256$" + "a" * 64).status_code, 400)

    def test_revoked_token_is_not_derived_from_the_job(self):
        self.client.get(f"/reprovision/answer/{self.answer}/user-data")
        self.assertFalse(Host.objects.by_token(f"revoked-{self.job.id}").exists())

    def test_enrol_requires_both_fields(self):
        self._installing()
        self.assertEqual(self._enrol(agent_token="").status_code, 400)
        self.assertEqual(self._enrol(enroll_token="").status_code, 400)

    def test_enrol_with_an_unknown_token_is_refused(self):
        self._installing()
        self.assertEqual(self._enrol(enroll_token="nope").status_code, 403)

    def test_enrol_clears_stale_os_state(self):
        from apps.hosts.models import HostInventory

        HostInventory.objects.create(host=self.host, os_name="Old Linux")
        self._installing()
        self._enrol()
        self.assertFalse(HostInventory.objects.filter(host=self.host).exists())

    def test_enrol_advances_the_job_to_enrolling(self):
        self._installing()
        self._enrol()
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, S.ENROLLING)


class InstallTreeTests(TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tree = Path(self.tmp.name) / "tree"
        (tree / "casper").mkdir(parents=True)
        (tree / "casper" / "filesystem.squashfs").write_bytes(b"payload")
        (Path(self.tmp.name) / "secret.txt").write_bytes(b"do not serve me")
        self.image = OSImage.objects.create(
            name="U", os_family=OSImage.Family.UBUNTU, version="24.04",
            architecture="x86_64", sha256="f" * 64,
            status=OSImage.Status.READY, tree_path=str(tree))

    def test_serves_a_file_from_the_tree(self):
        resp = self.client.get(
            f"/reprovision/tree/{self.image.id}/casper/filesystem.squashfs")
        self.assertEqual(resp.status_code, 200)

    def test_traversal_out_of_the_tree_is_refused(self):
        resp = self.client.get(
            f"/reprovision/tree/{self.image.id}/../secret.txt")
        self.assertEqual(resp.status_code, 404)

    def test_a_symlink_out_of_the_tree_is_not_followed(self):
        # The endpoint needs no login: a link in the tree must not make it a
        # reader for files outside it.
        from pathlib import Path
        tree = Path(self.image.tree_path)
        (tree / "casper" / "leak").symlink_to(Path(self.tmp.name) / "secret.txt")
        resp = self.client.get(f"/reprovision/tree/{self.image.id}/casper/leak")
        self.assertEqual(resp.status_code, 404)

    def test_a_symlink_inside_the_tree_still_serves(self):
        from pathlib import Path
        tree = Path(self.image.tree_path)
        (tree / "alias").symlink_to(tree / "casper")
        resp = self.client.get(f"/reprovision/tree/{self.image.id}/alias/filesystem.squashfs")
        self.assertEqual(resp.status_code, 200)

    def test_unready_image_is_not_served(self):
        self.image.status = OSImage.Status.IMPORTING
        self.image.save(update_fields=["status"])
        resp = self.client.get(
            f"/reprovision/tree/{self.image.id}/casper/filesystem.squashfs")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_image_404s(self):
        resp = self.client.get(f"/reprovision/tree/{uuid.uuid4()}/x")
        self.assertEqual(resp.status_code, 404)
