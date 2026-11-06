"""Vigil's own health for the About page (S3 05c).

Instance admins only: the counts cover every site, so the view runs its reads
in a system scope and refuses anyone who is not an administrator of the whole
install. The numbers are ``/metrics``' numbers, from the same ``collect()``.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsInstanceAdmin
from vigil import scoping, signing
from vigil.metrics_endpoint import collect


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsInstanceAdmin])
def instance_health(request):
    """Vigil's own health for the About page. Instance admins only: the counts
    cover every site."""
    with scoping.system(reason="instance health: the counts are install-wide"):
        data = collect()
        data["database_bytes"] = _database_bytes()
        data["signing_key"] = _signing_key()
    return Response(data)


def _database_bytes():
    from django.db import connection

    if connection.vendor != "postgresql":
        return None
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            return cur.fetchone()[0]
    except Exception:  # noqa: BLE001 - the page shows the rest of the health without it
        return None


def _signing_key():
    from apps.hosts.management.commands.signing_key import _approved_recent

    nxt = signing.next_signing_key()
    sk = {"active": signing.get_public_key_b64(), "next": signing.public_b64(nxt) if nxt else None}
    if nxt:
        hosts = list(_approved_recent())
        sk["acknowledged"] = sum(1 for h in hosts if h.signing_key_ack == sk["next"])
        sk["hosts"] = len(hosts)
    return sk
