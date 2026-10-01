"""Approve-each-change mode: a run proposes, a person disposes.

A run in approve mode records what it would change per host as a pending
PolicyChange. Approving dispatches the policy's compiled task to exactly
those hosts (all at once, with the window's expiry — the person approving
is the gate a wave ladder would otherwise be). Rejecting closes the row; the
next run proposes it again if the host is still drifted.
"""

from __future__ import annotations

from django.db import transaction
from django.utils.timezone import now

from .models import PolicyChange


def queue_changes(policy, drifted_hosts: list[dict]) -> int:
    """Replace each host's pending row with this run's changes."""
    with transaction.atomic():
        host_ids = [row["host_id"] for row in drifted_hosts]
        PolicyChange.objects.filter(policy=policy, state=PolicyChange.State.PENDING,
                                    host_id__in=host_ids).delete()
        PolicyChange.objects.bulk_create([
            PolicyChange(policy=policy, host_id=row["host_id"], changes=row["changes"])
            for row in drifted_hosts
        ])
    return len(drifted_hosts)


def decide(ids, *, approve: bool, user) -> dict:
    """Approve (and dispatch) or reject pending changes by id."""
    from apps.hosts.models import Host

    from .compile import compile_policy
    from .run import dispatch_to_hosts, high_risk_refusal

    pending = list(PolicyChange.objects.filter(pk__in=ids, state=PolicyChange.State.PENDING)
                   .select_related("policy"))
    stamp = now()
    if not approve:
        PolicyChange.objects.filter(pk__in=[c.pk for c in pending]).update(
            state=PolicyChange.State.REJECTED, decided_by=user, decided_at=stamp)
        return {"rejected": len(pending)}

    by_policy: dict = {}
    for change in pending:
        by_policy.setdefault(change.policy, []).append(change)
    dispatched = refused = 0
    for policy, changes in by_policy.items():
        definition = compile_policy(policy)
        state = PolicyChange.State.APPROVED
        if definition is not None and high_risk_refusal(policy, definition):
            refused += len(changes)   # stays pending until the policy is opted in
            continue
        if definition is not None:
            hosts = list(Host.objects.filter(pk__in=[c.host_id for c in changes]))
            dispatch_to_hosts(policy, definition, hosts, user=user)
            dispatched += len(hosts)
            state = PolicyChange.State.DISPATCHED
        PolicyChange.objects.filter(pk__in=[c.pk for c in changes]).update(
            state=state, decided_by=user, decided_at=stamp)
    result = {"approved": len(pending) - refused, "dispatched": dispatched}
    if refused:
        result["refused"] = refused
    return result
