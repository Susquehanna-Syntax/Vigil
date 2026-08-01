import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.reprovision import jobs
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob

S = RebuildJob.State


def make_job(state=S.PENDING, deadline_minutes=60):
    image = OSImage.objects.create(
        name="Ubuntu", os_family=OSImage.Family.UBUNTU, version="24.04",
        architecture="x86_64", sha256="d" * 64, status=OSImage.Status.READY)
    profile = InstallProfile.objects.create(
        name="std", image=image, disk_target="/dev/sda", admin_username="v")
    host = Host.objects.create(hostname="h1", agent_token=f"tok-{uuid.uuid4()}",
                               status=Host.Status.ONLINE)
    return RebuildJob.objects.create(
        host=host, image=image, profile=profile, state=state,
        deadline=now() + timedelta(minutes=deadline_minutes))


class TransitionTests(TestCase):
    def test_legal_transition_advances(self):
        job = jobs.advance(make_job(S.PENDING), S.STAGING)
        self.assertEqual(job.state, S.STAGING)

    def test_illegal_transition_raises(self):
        with self.assertRaises(jobs.InvalidTransition):
            jobs.advance(make_job(S.PENDING), S.COMPLETED)

    def test_terminal_states_are_final(self):
        for terminal in (S.COMPLETED, S.FAILED, S.ABORTED, S.TIMED_OUT):
            with self.assertRaises(jobs.InvalidTransition):
                jobs.advance(make_job(terminal), S.STAGING)

    def test_abort_is_unreachable_after_the_reboot_is_issued(self):
        # Past the point of no return the honest answer is that it is out of
        # our hands — see docs/reprovisioning.md §3.3.
        for state in (S.REBOOTING, S.INSTALLING, S.ENROLLING):
            with self.assertRaises(jobs.InvalidTransition):
                jobs.advance(make_job(state), S.ABORTED)

    def test_abort_is_reachable_before_the_reboot(self):
        for state in (S.PENDING, S.STAGING, S.STAGED):
            self.assertEqual(jobs.advance(make_job(state), S.ABORTED).state,
                             S.ABORTED)

    def test_failure_reason_is_recorded(self):
        job = jobs.advance(make_job(S.STAGING), S.FAILED, reason="checksum")
        self.assertEqual(job.failure_reason, "checksum")

    def test_state_changed_at_moves(self):
        job = make_job(S.PENDING)
        before = job.state_changed_at
        advanced = jobs.advance(job, S.STAGING)
        self.assertGreater(advanced.state_changed_at, before)

    def test_every_state_has_a_transition_entry(self):
        # A state missing from the table would silently become terminal.
        self.assertEqual(set(jobs.ALLOWED_TRANSITIONS),
                         {s.value for s in S})

    def test_no_transition_skips_the_point_of_no_return(self):
        # Nothing may reach INSTALLING except from REBOOTING: the answer-file
        # fetch is the only thing allowed to cross that line.
        sources = [s for s, targets in jobs.ALLOWED_TRANSITIONS.items()
                   if S.INSTALLING in targets]
        self.assertEqual(sources, [S.REBOOTING])


class MaintenanceWindowTests(TestCase):
    def test_rebooting_opens_the_window(self):
        job = jobs.advance(make_job(S.STAGED), S.REBOOTING)
        job.host.refresh_from_db()
        self.assertEqual(job.host.maintenance_until, job.deadline)

    def test_terminal_state_clears_the_window(self):
        job = jobs.advance(make_job(S.STAGED), S.REBOOTING)
        jobs.advance(job, S.FAILED, reason="installer never booted")
        job.host.refresh_from_db()
        self.assertIsNone(job.host.maintenance_until)


class TokenTests(TestCase):
    def test_mint_stores_only_hashes(self):
        job = make_job()
        answer, enroll = jobs.mint_tokens(job)
        job.refresh_from_db()
        self.assertNotIn(answer, (job.answer_token_hash, job.enroll_token_hash))
        self.assertNotIn(enroll, (job.answer_token_hash, job.enroll_token_hash))
        self.assertEqual(job.answer_token_hash, jobs.hash_token(answer))
        self.assertEqual(job.enroll_token_hash, jobs.hash_token(enroll))

    def test_tokens_are_distinct_and_long(self):
        answer, enroll = jobs.mint_tokens(make_job())
        self.assertNotEqual(answer, enroll)
        self.assertGreaterEqual(len(answer), 40)
        self.assertGreaterEqual(len(enroll), 40)

    def test_tokens_differ_between_jobs(self):
        a1, e1 = jobs.mint_tokens(make_job())
        a2, e2 = jobs.mint_tokens(make_job())
        self.assertNotEqual(a1, a2)
        self.assertNotEqual(e1, e2)

    def test_enrol_token_round_trips_through_the_cache(self):
        job = make_job()
        _answer, enroll = jobs.mint_tokens(job)
        jobs.stash_enroll_token(job, enroll)
        self.assertEqual(jobs.pop_enroll_token(job), enroll)

    def test_missing_stash_returns_empty_not_none(self):
        self.assertEqual(jobs.pop_enroll_token(make_job()), "")


class DeadlineSweepTests(TestCase):
    def test_expired_transient_job_times_out(self):
        job = make_job(S.INSTALLING, deadline_minutes=-1)
        self.assertEqual(jobs.sweep_deadlines(), 1)
        job.refresh_from_db()
        self.assertEqual(job.state, S.TIMED_OUT)

    def test_terminal_job_is_left_alone(self):
        job = make_job(S.COMPLETED, deadline_minutes=-1)
        self.assertEqual(jobs.sweep_deadlines(), 0)
        job.refresh_from_db()
        self.assertEqual(job.state, S.COMPLETED)

    def test_live_job_is_left_alone(self):
        make_job(S.INSTALLING, deadline_minutes=30)
        self.assertEqual(jobs.sweep_deadlines(), 0)

    def test_timeout_clears_the_maintenance_window(self):
        # Otherwise a failed rebuild silences the host's alerts forever.
        job = make_job(S.INSTALLING, deadline_minutes=-1)
        job.host.maintenance_until = now() + timedelta(hours=5)
        job.host.save(update_fields=["maintenance_until"])
        jobs.sweep_deadlines()
        job.host.refresh_from_db()
        self.assertIsNone(job.host.maintenance_until)

    def test_sweep_records_why(self):
        job = make_job(S.REBOOTING, deadline_minutes=-1)
        jobs.sweep_deadlines()
        job.refresh_from_db()
        self.assertIn("deadline", job.failure_reason.lower())
