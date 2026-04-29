"""Background jobs for the hosts app.

Currently:
    - import_ad_computers: connects to the configured AD server, fetches all
      computer objects under the configured OU, and creates/updates ``Host``
      records (status=PENDING — they still wait for normal agent enrollment
      before they can run tasks). OU segments become auto-tags.
"""

from __future__ import annotations

import logging
import secrets

from celery import shared_task
from django.utils.timezone import now

from .auto_tags import derive_auto_tags, merge_auto_tags
from .crypto import decrypt_secret
from .models import ADConfig, Host

logger = logging.getLogger(__name__)


@shared_task(name="hosts.import_ad_computers")
def import_ad_computers() -> dict:
    """Sync computer objects from Active Directory into the Host table.

    Returns a summary dict: ``{"created": N, "updated": M, "errors": [...]}``.
    The task is intentionally idempotent — re-running it converges on the
    current AD state without duplicating hosts.
    """
    config = ADConfig.objects.filter(enabled=True).order_by("-updated_at").first()
    if config is None:
        return {"error": "No enabled ADConfig", "created": 0, "updated": 0}

    try:
        # ldap3 is an optional dependency — fail loudly if it isn't installed
        from ldap3 import ALL, Connection, Server  # type: ignore
    except ImportError:
        config.last_sync = now()
        config.last_sync_status = "ldap3 not installed (pip install ldap3)"
        config.save(update_fields=["last_sync", "last_sync_status"])
        return {"error": "ldap3 not installed", "created": 0, "updated": 0}

    password = decrypt_secret(config.bind_password_encrypted)
    if not password:
        config.last_sync = now()
        config.last_sync_status = "Bind password missing or undecryptable"
        config.save(update_fields=["last_sync", "last_sync_status"])
        return {"error": "bind password unavailable", "created": 0, "updated": 0}

    server = Server(config.ldap_url, get_info=ALL)
    search_base = config.computer_ou or config.base_dn
    if not search_base:
        config.last_sync = now()
        config.last_sync_status = "No search base / computer OU configured"
        config.save(update_fields=["last_sync", "last_sync_status"])
        return {"error": "search base missing", "created": 0, "updated": 0}

    created = 0
    updated = 0
    errors: list[str] = []

    try:
        with Connection(server, user=config.bind_dn, password=password, auto_bind=True) as conn:
            conn.search(
                search_base=search_base,
                search_filter="(objectClass=computer)",
                attributes=["cn", "dNSHostName", "operatingSystem", "distinguishedName"],
            )
            for entry in conn.entries:
                cn = str(getattr(entry, "cn", "") or "")
                dn = str(getattr(entry, "distinguishedName", "") or "")
                hostname = str(getattr(entry, "dNSHostName", "") or "") or cn
                os_name = str(getattr(entry, "operatingSystem", "") or "")
                if not hostname:
                    continue

                host = Host.objects.filter(hostname__iexact=hostname).first()
                if host is None:
                    # New AD-imported host — pending real agent enrollment.
                    host = Host(
                        hostname=hostname,
                        os=os_name[:100],
                        agent_token=f"ad-imported-{secrets.token_urlsafe(20)}",
                        status=Host.Status.PENDING,
                    )
                    host.tags = derive_auto_tags(host, ad_distinguished_name=dn)
                    host.save()
                    created += 1
                else:
                    if os_name and not host.os:
                        host.os = os_name[:100]
                    host.tags = merge_auto_tags(host, ad_distinguished_name=dn)
                    host.save(update_fields=["os", "tags", "updated_at"])
                    updated += 1
    except Exception as exc:
        logger.exception("AD sync failed")
        errors.append(str(exc))
        config.last_sync = now()
        config.last_sync_status = f"FAILED: {exc}"[:255]
        config.save(update_fields=["last_sync", "last_sync_status"])
        return {"error": str(exc), "created": created, "updated": updated, "errors": errors}

    config.last_sync = now()
    config.last_sync_status = f"OK: {created} created, {updated} updated"
    config.save(update_fields=["last_sync", "last_sync_status"])
    return {"created": created, "updated": updated, "errors": errors}
