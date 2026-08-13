"""What happens when a rebuilt host says hello (docs/reprovisioning.md §10)."""
import logging

from vigil import hooks

from . import jobs
from .models import RebuildJob

logger = logging.getLogger("vigil.reprovision")


def _apply_completion_tags(job, host) -> None:
    """Tag the rebuilt host: the profile's standing tags, then this job's
    one-off, in that order and without duplicates.

    ``agent:*`` is reserved for agent-advertised tags so a rogue agent cannot
    impersonate operator-set ones. Both entry points reject it too; this
    writes straight to the host, so it does not rely on either.
    """
    profile = job.profile
    candidates = list(getattr(profile, "completion_tags", None) or [])
    candidates.append(job.completion_tag or "")

    existing = list(host.tags or [])
    added = []
    for candidate in candidates:
        tag = str(candidate).strip()
        if not tag or tag.startswith("agent:"):
            continue
        if tag not in existing and tag not in added:
            added.append(tag)
    if added:
        host.tags = existing + added
        host.save(update_fields=["tags"])


def complete_if_rebuilding(host):
    """Finish the rebuild if this check-in is the one we were waiting for.

    Never raises: a broken baseline must not stop a host checking in, and it
    must not strand the job in ENROLLING either — the job completes even when
    the post-rebuild dispatch fails, with the failure logged.
    """
    job = RebuildJob.objects.filter(
        host=host, state=RebuildJob.State.ENROLLING).select_related(
            "post_baseline").first()
    if job is None:
        return None

    _apply_completion_tags(job, host)

    if job.post_baseline is not None:
        try:
            from apps.baselines.models import dispatch_to_host

            dispatch_to_host(host, baselines=[job.post_baseline])
            run_id = host.tasks.filter(run__isnull=False).order_by(
                "-created_at").values_list("run", flat=True).first()
            if run_id:
                job.post_run_id = run_id
                job.save(update_fields=["post_run"])
        except Exception:
            logger.exception(
                "rebuild %s: post-rebuild baseline dispatch failed", job.id)

    try:
        jobs.advance(job, RebuildJob.State.COMPLETED)
        hooks.emit("rebuild_completed", job=job, host=host)
    except Exception:
        logger.exception("rebuild %s: could not mark completed", job.id)
        return None
    return job
