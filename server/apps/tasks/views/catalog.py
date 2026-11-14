"""apps.tasks.views: catalog views (split from the old single module, unchanged)."""

from drf_spectacular.utils import extend_schema

from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403
from ..api_schema import (
    TasksCommunityCardSerializer,
    TasksDetailSerializer,
    TasksErrorSerializer,
    TasksForkPlanSerializer,
    TasksForkResponseSerializer,
)


@extend_schema(
    responses={
        200: TasksCommunityCardSerializer(many=True),
        404: TasksErrorSerializer,
        502: TasksErrorSerializer,
    }
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def community_templates(request, kind: str = "tasks"):
    """List one kind of community content from the public GitHub repo (cached).

    Cached per kind: the three directories are fetched independently, so a
    slow or empty automations/ does not hold up the tasks tab.
    """
    from django.core.cache import cache

    if kind not in COMMUNITY_KINDS:
        return Response({"error": f"unknown content kind {kind!r}"}, status=404)

    cache_key = f"{_COMMUNITY_CACHE_KEY}:{kind}"
    force = request.query_params.get("refresh") == "1"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)
    try:
        templates = _fetch_community_templates(kind)
    except Exception:
        return Response(
            {
                "error": "Community repo unreachable — check the server's internet access"
            },
            status=502,
        )
    cache.set(cache_key, templates, _COMMUNITY_CACHE_TTL)
    return Response(templates)


@extend_schema(
    request=None,
    responses={
        200: TasksForkPlanSerializer,
        403: TasksErrorSerializer,
        404: TasksErrorSerializer,
        502: TasksErrorSerializer,
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def community_fork_plan(request, kind: str, filename: str):
    """What forking this catalog item would create, without creating it."""
    from ..authz import author_denied

    denied = author_denied(request.user)
    if denied:
        return denied
    if kind not in COMMUNITY_KINDS:
        return Response({"error": f"unknown content kind {kind!r}"}, status=404)
    try:
        plan = _plan_fork(kind, filename, request.user)
    except Exception:
        return Response({"error": "Community repo unreachable"}, status=502)
    if "error" in plan:
        return Response(plan, status=404)
    return Response(
        {
            "name": plan["item"]["name"],
            "have": plan["have"],
            "needs": [
                {
                    **{k: n[k] for k in ("kind", "slug", "name", "found", "have")},
                    "uid": (n.get("source") or {}).get("uid", ""),
                }
                for n in plan["needs"]
            ],
        }
    )


@extend_schema(
    request=None,
    responses={
        200: TasksForkResponseSerializer,
        201: TasksForkResponseSerializer,
        400: TasksDetailSerializer,
        403: TasksErrorSerializer,
        404: TasksErrorSerializer,
        502: TasksErrorSerializer,
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def community_fork(request, kind: str, filename: str):
    """Fork a catalog item and everything it needs, in dependency order.

    Tasks first, then playbooks, then the item itself — anything already held
    is skipped rather than duplicated. The whole thing is one transaction: a
    half-forked playbook missing its third step is not a state worth leaving
    an operator in.
    """
    from apps.automations.views import automation_from_yaml
    from apps.playbooks.views import playbook_from_yaml

    from ..authz import author_denied

    # Forking creates tasks, playbooks and automations: the same role gate as
    # every other way of creating them, and as community_fork_plan.
    denied = author_denied(request.user)
    if denied:
        return denied
    if kind not in COMMUNITY_KINDS:
        return Response({"error": f"unknown content kind {kind!r}"}, status=404)
    try:
        plan = _plan_fork(kind, filename, request.user)
    except Exception:
        return Response({"error": "Community repo unreachable"}, status=502)
    if "error" in plan:
        return Response(plan, status=404)

    if plan["have"]:
        # Already held. A no-op with a clear answer beats a duplicate-name
        # error, which reads as a failure when nothing is actually wrong.
        return Response({"created": [], "already": True, "name": plan["item"]["name"]})

    missing = [n["slug"] for n in plan["needs"] if not n["found"]]
    if missing:
        return Response(
            {
                "detail": "The catalog is missing files this references: "
                + ", ".join(sorted(set(missing)))
            },
            status=400,
        )

    created = []
    with transaction.atomic():
        # Tasks before playbooks: a playbook import resolves its steps against
        # the library, so its tasks have to be in there first.
        for need in [n for n in plan["needs"] if n["kind"] == "tasks"]:
            if need["have"]:
                continue
            _fork_task_from_catalog(need["source"], request.user)
            created.append({"kind": "tasks", "name": need["name"]})

        for need in [n for n in plan["needs"] if n["kind"] == "playbooks"]:
            if need["have"]:
                continue
            sub = _import_via(playbook_from_yaml, request, need["source"])
            if sub.status_code >= 400:
                transaction.set_rollback(True)
                return sub
            created.append({"kind": "playbooks", "name": need["name"]})

        item = plan["item"]
        if kind == "tasks":
            definition = _fork_task_from_catalog(item, request.user)
            created.append({"kind": "tasks", "name": definition.name})
        else:
            view = playbook_from_yaml if kind == "playbooks" else automation_from_yaml
            result = _import_via(view, request, item)
            if result.status_code >= 400:
                transaction.set_rollback(True)
                return result
            created.append({"kind": kind, "name": item["name"]})

    return Response({"created": created}, status=status.HTTP_201_CREATED)
