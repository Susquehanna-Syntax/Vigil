"""apps.hosts.views: tags views (split from the old single module, unchanged)."""
from ._common import *  # noqa: F401,F403


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def tag_collection(request):
    """List tags with their usage counts, or create one."""
    from ..models import Tag

    if request.method == "GET":
        tags = _visible_tags(request.user).prefetch_related("hosts")
        return Response([_tag_payload(t) for t in tags])

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": "Admin role required."}, status=403)
    # S1 04h: a site admin makes tags in one of their sites; a global tag is
    # an instance admin's.
    site_id, denied = scoping.site_for_write(request.user, request.data.get("site_id"))
    if denied:
        return denied

    name = str(request.data.get("name") or "")
    if not name.strip():
        return Response({"name": ["A tag needs a name."]}, status=400)
    if Tag.is_reserved(name):
        # os:/pkg:/arch: are rebuilt from inventory on every check-in and
        # agent:* exists so a compromised agent cannot impersonate an
        # operator tag. Neither is an operator's to create.
        return Response(
            {"name": [f"{name!r} uses a reserved prefix. Those tags are set by "
                      f"Vigil itself, not by hand."]},
            status=400,
        )
    if Tag.objects.filter(key=Tag.canonical_key(name)).exists():
        return Response({"name": [f"A tag matching {name!r} already exists."]},
                        status=400)
    tag, _ = Tag.get_or_create_by_name(name)
    tag.site_id = site_id
    if request.data.get("description"):
        tag.description = str(request.data["description"])[:255]
    tag.save(update_fields=["site_id", "description"])
    return Response(_tag_payload(tag), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def tag_detail(request, tag_id):
    """Read, rename, or delete a tag."""
    from ..models import Tag

    # Another site's tag answers 404, as if it did not exist (S1 04h).
    tag = get_object_or_404(_visible_tags(request.user), pk=tag_id)
    if request.method == "GET":
        return Response(_tag_payload(tag))

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": "Admin role required."}, status=403)
    # A site's tag is its admins' to change; a global one an instance admin's.
    denied = scoping.write_denied(request.user, tag)
    if denied:
        return denied

    if tag.kind != Tag.Kind.MANUAL:
        return Response(
            {"detail": f"{tag.name!r} is maintained by Vigil, not by hand. "
                       f"Auto and agent tags are rebuilt from what the host reports."},
            status=400,
        )

    if request.method == "DELETE":
        # Refuse while anything still selects on it, rather than silently
        # emptying a wave. The message names what is using it.
        # Counted across every site: a tag is install-wide, so rows the caller
        # cannot see still use it. A scoped count would let a site admin
        # delete a tag that another site's hosts or playbooks select on.
        users = []
        for rel, label in (("hosts", "host"), ("waves", "wave"), ("playbooks", "playbook"),
                           ("automations_by_event", "automation (event)"),
                           ("automations_by_target", "automation (target)")):
            manager = getattr(tag, rel, None)
            count = _count_everywhere(manager) if manager is not None else 0
            if count:
                users.append(f"{count} {label}(s)")
        if users:
            return Response(
                {"detail": f"{tag.name!r} is still used by " + ", ".join(users)
                           + ". Remove it from those first."},
                status=409,
            )
        tag.delete()
        return Response(status=204)

    new_name = str(request.data.get("name") or "").strip()
    if new_name:
        if Tag.is_reserved(new_name):
            return Response({"name": ["That prefix is reserved for Vigil."]},
                            status=400)
        clash = Tag.objects.filter(key=Tag.canonical_key(new_name)).exclude(pk=tag.pk)
        if clash.exists():
            return Response({"name": [f"A tag matching {new_name!r} already exists."]},
                            status=400)
        # Renaming the row renames it everywhere at once — that is the whole
        # point of rows over strings. The mirrored string lists are updated
        # too, so the two stay consistent until the strings are dropped.
        old_key = tag.key
        tag.name = new_name
        tag.save()
        _rename_in_string_mirrors(old_key, new_name)
    if "description" in request.data:
        tag.description = str(request.data.get("description") or "")[:255]
        tag.save(update_fields=["description"])
    return Response(_tag_payload(tag))
