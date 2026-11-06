from celery import shared_task

from vigil import scoping


@shared_task(name="instance.backup_tick")
@scoping.system_job(reason="background job: a backup covers the whole install")
def backup_tick():
    """Beat runs this hourly; it backs up only when backups are on and this
    is the configured hour (apps.instance.backup.due)."""
    from .backup import due, run_backup

    if not due():
        return "not due"
    return run_backup().state
