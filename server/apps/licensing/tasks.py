from celery import shared_task

from vigil import licensing


@shared_task(name="licensing.refresh")
def refresh_license():
    """Beat runs this every few hours; it calls out only when a license is due
    (vigil.licensing.refresh_due), so most runs send nothing."""
    return licensing.refresh_license()["result"]
