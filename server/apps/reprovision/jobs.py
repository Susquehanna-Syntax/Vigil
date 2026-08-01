"""The rebuild state machine. See docs/reprovisioning.md §3.3.

Every transition is explicit: an unlisted move raises rather than silently
recording a state nobody designed for. The machine is organised around one
line — the answer-file fetch — before which everything is reversible and
after which the disk is being destroyed.
"""
import hashlib
import logging
import secrets

from django.core.cache import cache
from django.db import transaction
from django.utils.timezone import now

from vigil import hooks

from .models import RebuildJob

logger = logging.getLogger("vigil.reprovision")

S = RebuildJob.State

#: Legal moves. ABORTED is deliberately absent from REBOOTING onwards: once
#: the reboot is issued there is nothing left to abort. Note that INSTALLING
#: is reachable only from REBOOTING — the answer-file fetch is the sole thing
#: permitted to cross the point of no return.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    S.PENDING: frozenset({S.STAGING, S.ABORTED, S.FAILED, S.TIMED_OUT}),
    S.STAGING: frozenset({S.STAGED, S.ABORTED, S.FAILED, S.TIMED_OUT}),
    S.STAGED: frozenset({S.REBOOTING, S.ABORTED, S.FAILED, S.TIMED_OUT}),
    S.REBOOTING: frozenset({S.INSTALLING, S.FAILED, S.TIMED_OUT}),
    S.INSTALLING: frozenset({S.ENROLLING, S.FAILED, S.TIMED_OUT}),
    S.ENROLLING: frozenset({S.COMPLETED, S.FAILED, S.TIMED_OUT}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.ABORTED: frozenset(),
    S.TIMED_OUT: frozenset(),
}


class InvalidTransition(Exception):
    """An undesigned state move was attempted."""


def hash_token(raw: str) -> str:
    """Hash a token for storage.

    Plain SHA-256 is correct here: these are 256-bit random values, so there
    is no low-entropy secret to brute-force and no reason to pay for a slow
    KDF on a path the installer hits mid-rebuild.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mint_tokens(job) -> tuple[str, str]:
    """Generate the answer-file and enrolment tokens.

    Returns the plaintext pair for embedding in the boot cmdline and the
    answer file. Only the hashes are persisted, so a database read never
    yields a usable credential (§4.2).
    """
    answer = secrets.token_urlsafe(32)
    enroll = secrets.token_urlsafe(32)
    job.answer_token_hash = hash_token(answer)
    job.enroll_token_hash = hash_token(enroll)
    job.save(update_fields=["answer_token_hash", "enroll_token_hash"])
    return answer, enroll


def stash_enroll_token(job, raw: str) -> None:
    """Hold the plaintext enrolment token until the installer fetches the
    answer file.

    Cache, not database: it must not outlive the job, and a database read
    must never yield a usable credential.
    """
    ttl = int((job.deadline - now()).total_seconds())
    cache.set(f"reprovision:enroll:{job.id}", raw, timeout=max(ttl, 60))


def pop_enroll_token(job) -> str:
    return cache.get(f"reprovision:enroll:{job.id}", "")


def advance(job, to_state: str, *, reason: str = "") -> RebuildJob:
    """Move *job* to *to_state*, or raise InvalidTransition."""
    permitted = ALLOWED_TRANSITIONS.get(job.state, frozenset())
    if to_state not in permitted:
        raise InvalidTransition(
            f"{job.state} → {to_state} is not a designed transition")

    previous = job.state
    job.state = to_state
    job.state_changed_at = now()
    fields = ["state", "state_changed_at"]
    if reason:
        job.failure_reason = reason
        fields.append("failure_reason")
    job.save(update_fields=fields)

    if to_state == S.REBOOTING:
        _open_maintenance(job)
    elif to_state in RebuildJob.TERMINAL_STATES:
        _close_maintenance(job)

    hooks.emit("rebuild_state_changed", job=job, previous=previous, reason=reason)
    logger.info("rebuild %s: %s → %s%s", job.id, previous, to_state,
                f" ({reason})" if reason else "")
    return job


def _open_maintenance(job) -> None:
    """Suppress this host's alerts for the length of the rebuild. Without it
    the first real rebuild pages everyone."""
    job.host.maintenance_until = job.deadline
    job.host.save(update_fields=["maintenance_until"])


def _close_maintenance(job) -> None:
    """Drop the suppression window. Without this a failed rebuild silences
    the host's alerts indefinitely."""
    if job.host.maintenance_until is not None:
        job.host.maintenance_until = None
        job.host.save(update_fields=["maintenance_until"])


def sweep_deadlines() -> int:
    """Time out every live job past its deadline. Returns how many moved."""
    stale = RebuildJob.objects.filter(deadline__lt=now()).exclude(
        state__in=RebuildJob.TERMINAL_STATES).select_related("host")
    moved = 0
    for job in stale:
        with transaction.atomic():
            try:
                advance(job, S.TIMED_OUT, reason="Deadline exceeded")
                moved += 1
            except InvalidTransition:
                logger.warning("rebuild %s could not time out from %s",
                               job.id, job.state)
    return moved
