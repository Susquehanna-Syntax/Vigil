from celery import shared_task

from vigil import licensing
from vigil import scoping


@shared_task(name="licensing.refresh")
@scoping.system_job(reason="background job: the licence is install-wide")
def refresh_license():
    """Beat runs this every few hours; it calls out only when a license is due
    (vigil.licensing.refresh_due), so most runs send nothing."""
    return licensing.refresh_license()["result"]
