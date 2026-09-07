"""Periodic reconciliation for auto-enrolling playbooks."""

import logging

from celery import shared_task

logger = logging.getLogger("vigil.playbooks")

#: Dispatches per pass. A playbook that has just been switched on covers a
#: fleet over a few minutes rather than starting a package operation on every
#: machine at once, and a misconfigured one is noticed before it has run
#: everywhere.
RECONCILE_LIMIT = 25


@shared_task(name="playbooks.reconcile")
def reconcile_playbooks():
    from .models import reconcile

    dispatched = reconcile(limit=RECONCILE_LIMIT)
    if dispatched:
        logger.info("playbook reconcile dispatched %d task(s)", dispatched)
    return f"playbook reconcile: {dispatched} dispatched"
