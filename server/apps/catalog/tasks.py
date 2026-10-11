from celery import shared_task


@shared_task(name="catalog.sync")
def sync_catalog() -> str:
    """Daily: refresh the Store's package catalog — only when an admin has
    turned it on (VIGIL_CATALOG_SYNC); Vigil never reaches out on its own."""
    from apps.instance.config import setting

    if not setting("VIGIL_CATALOG_SYNC"):
        return "catalog sync is off"
    from .sync import sync_all
    rows = sync_all()
    return "; ".join(f"{r.source}{':' + r.release if r.release else ''} {r.count}"
                     f"{' (' + r.error + ')' if r.error else ''}" for r in rows) or "nothing to sync"
