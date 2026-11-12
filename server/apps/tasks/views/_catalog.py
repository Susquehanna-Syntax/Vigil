"""apps.tasks.views: shared helpers (catalog), moved unchanged."""
from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403


def _fetch_community_templates(kind: str = "tasks") -> list[dict]:
    """Pull and parse the YAML in one of the community repo's directories.

    Invalid or unparsable files are skipped — the repo gates quality through
    PR review, but a bad merge must not blank the whole tab.
    """
    import requests as _requests

    if kind not in _COMMUNITY_PARSERS:
        raise ValueError(f"unknown community kind {kind!r}")
    to_card = _COMMUNITY_PARSERS[kind]

    listing = _requests.get(
        f"https://api.github.com/repos/{VIGIL_COMMUNITY_REPO}/contents/{kind}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if listing.status_code == 404:
        # Repo empty, or that directory not created yet — a valid "nothing
        # here" state rather than an error.
        return []
    listing.raise_for_status()

    entries = [
        e for e in listing.json()
        if isinstance(e, dict)
        and e.get("type") == "file"
        and e.get("name", "").endswith((".yaml", ".yml"))
        and e.get("download_url")
    ][:_COMMUNITY_MAX_TEMPLATES]

    templates = []
    for entry in entries:
        try:
            raw = _requests.get(entry["download_url"], timeout=10)
            raw.raise_for_status()
            card = to_card(raw.text)
        except Exception:
            continue
        templates.append({
            "kind": kind,
            "filename": entry["name"],
            "html_url": entry.get("html_url", ""),
            "yaml_source": raw.text,
            **card,
        })
    return templates


def community_names_by_slug(kind: str) -> dict[str, str]:
    """Map each community file's slug to the ``name`` inside it.

    A slug in the repo is the **filename**, and the repo does not enforce that
    the filename equals ``slugify(name)`` — ``docker-prune-and-restart-unhealthy.yaml``
    is called "Docker Prune and Restart Unhealthy Container". So resolving a
    reference by slugifying library names alone would refuse to import a
    playbook whose task the operator demonstrably has.

    Reads the same server-side cache the Community tab fills, so the common
    path costs nothing. Returns ``{}`` when the repo is unreachable rather than
    raising: an unresolvable slug is already handled, and a network blip must
    not turn a working import into an error.
    """
    from django.core.cache import cache

    if kind not in COMMUNITY_KINDS:
        return {}
    cached = cache.get(f"{_COMMUNITY_CACHE_KEY}:{kind}")
    if cached is None:
        try:
            cached = _fetch_community_templates(kind)
        except Exception:
            return {}
        cache.set(f"{_COMMUNITY_CACHE_KEY}:{kind}", cached, _COMMUNITY_CACHE_TTL)
    names = {}
    for item in cached:
        stem = item.get("filename", "").rsplit(".", 1)[0]
        if stem and item.get("name"):
            names[stem] = item["name"]
    return names


def _library_index(model, user):
    """Index an operator's existing library by uid and by name."""
    from vigil import scoping

    rows = scoping.filter_by_site(model.objects.all(), user, cascade_global=True)
    by_uid, by_name = {}, {}
    for row in rows:
        if row.community_uid:
            by_uid.setdefault(str(row.community_uid), row)
        by_name.setdefault(row.name.strip().lower(), row)
    return by_uid, by_name


def _already_have(by_uid, by_name, uid, name):
    """The operator's existing copy of a catalog item, if they have one.

    When the catalog file carries a uid, that is the *only* thing consulted.
    Falling back to the name there would undo what uids are for: a task the
    operator wrote themselves that happens to also be called "Install Nginx"
    is a different task, and treating it as the catalog's would silently skip
    the fork and leave a playbook pointing at the wrong steps.

    The name pass exists for catalog files written before uids, which have no
    other identity to offer.
    """
    if uid:
        return by_uid.get(str(uid))
    return by_name.get((name or "").strip().lower())


def _catalog(kind):
    """The catalog for *kind*, indexed by slug and by uid."""
    from django.core.cache import cache

    cached = cache.get(f"{_COMMUNITY_CACHE_KEY}:{kind}")
    if cached is None:
        cached = _fetch_community_templates(kind)
        cache.set(f"{_COMMUNITY_CACHE_KEY}:{kind}", cached, _COMMUNITY_CACHE_TTL)
    by_slug, by_uid = {}, {}
    for item in cached:
        stem = item.get("filename", "").rsplit(".", 1)[0]
        if stem:
            by_slug[stem] = item
        if item.get("uid"):
            by_uid[str(item["uid"])] = item
    return by_slug, by_uid


def _find_in_catalog(by_slug, by_uid, ref):
    """Locate a referenced catalog file from a ``{slug, uid}`` reference."""
    uid = (ref.get("uid") or "").strip()
    if uid and uid in by_uid:
        return by_uid[uid]
    return by_slug.get(ref.get("slug", ""))


def _fork_task_from_catalog(item, user) -> TaskDefinition:
    """Create a library task from one catalog file."""
    definition = TaskDefinition(owner=user,
                                visibility=TaskDefinition.Visibility.PRIVATE,
                                content_source=TaskDefinition.ContentSource.COMMUNITY)
    _save_definition_from_yaml(definition, item["yaml_source"])
    definition.save()
    return definition


def _plan_fork(kind: str, filename: str, user) -> dict:
    """Work out what forking one catalog item would create.

    Returns the item, the ordered list of dependencies, and — for each — the
    operator's existing copy if they have one. Read-only: the same walk backs
    the preview on the card and the fork itself, so what the card promises is
    what the fork does.
    """
    from apps.playbooks.models import Playbook

    task_by_slug, task_by_uid = _catalog("tasks")
    item = None
    if kind == "tasks":
        item = task_by_slug.get(filename.rsplit(".", 1)[0])
    else:
        by_slug, _ = _catalog(kind)
        item = by_slug.get(filename.rsplit(".", 1)[0])
    if item is None:
        return {"error": f"{filename} is not in the catalog"}

    have_tasks_uid, have_tasks_name = _library_index(TaskDefinition, user)
    have_bl_uid, have_bl_name = _library_index(Playbook, user)

    needs = []
    for ref in item.get("requires") or []:
        if ref["kind"] == "tasks":
            source = _find_in_catalog(task_by_slug, task_by_uid, ref)
            existing = _already_have(have_tasks_uid, have_tasks_name,
                                     ref.get("uid"),
                                     source["name"] if source else ref["slug"])
        else:
            bl_by_slug, bl_by_uid = _catalog("playbooks")
            source = _find_in_catalog(bl_by_slug, bl_by_uid, ref)
            existing = _already_have(have_bl_uid, have_bl_name, ref.get("uid"),
                                     source["name"] if source else ref["slug"])
            # A playbook dependency drags its own tasks along.
            for inner in (source or {}).get("requires") or []:
                inner_src = _find_in_catalog(task_by_slug, task_by_uid, inner)
                needs.append({
                    "kind": "tasks", "slug": inner["slug"],
                    "name": inner_src["name"] if inner_src else inner["slug"],
                    "found": inner_src is not None,
                    "have": bool(_already_have(
                        have_tasks_uid, have_tasks_name, inner.get("uid"),
                        inner_src["name"] if inner_src else inner["slug"])),
                    "source": inner_src,
                })
        needs.append({
            "kind": ref["kind"], "slug": ref["slug"],
            "name": source["name"] if source else ref["slug"],
            "found": source is not None,
            "have": existing is not None,
            "source": source,
        })

    # Dedupe, keeping first occurrence: a playbook can use one task twice, and
    # two steps of an automation's playbook can share one.
    seen, ordered = set(), []
    for need in needs:
        key = (need["kind"], need["slug"])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(need)

    # Whether the operator already holds the item itself, not just its
    # dependencies. Without this the cascade skips every dependency, then tries
    # to import the item anyway and trips the duplicate-name check — which
    # reads as a failure when the honest answer is "you already have this".
    from apps.automations.models import Automation

    model = {"tasks": TaskDefinition, "playbooks": Playbook,
             "automations": Automation}[kind]
    have_uid, have_name = _library_index(model, user)
    mine = _already_have(have_uid, have_name, item.get("uid"), item["name"])
    return {"item": item, "needs": ordered, "have": mine is not None}


def _import_via(view, request, item):
    """Run one of the YAML import views against a catalog file.

    Reusing the view rather than its internals is deliberate: the eligibility
    rules and the TOTP gate on ``allow_high_risk`` live there, and a cascade
    fork must not be a way past either.
    """
    from rest_framework.test import APIRequestFactory, force_authenticate

    factory = APIRequestFactory()
    sub = factory.post("/", {"yaml": item["yaml_source"]}, format="json")
    force_authenticate(sub, user=request.user)
    return view(sub)


__all__ = ["_already_have", "_catalog", "_fetch_community_templates", "_find_in_catalog", "_fork_task_from_catalog", "_import_via", "_library_index", "_plan_fork", "community_names_by_slug"]
