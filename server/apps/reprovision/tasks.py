from pathlib import Path

from celery import shared_task
from vigil import scoping


@shared_task(name="reprovision.import_iso")
@scoping.system_job(reason="background job: importing an OS image, which belongs to no site")
def import_iso_task(image_id: str, iso_path: str) -> None:
    from .images import import_iso
    from .models import OSImage

    image = OSImage.objects.filter(pk=image_id).first()
    if image is None:
        return
    import_iso(image, Path(iso_path))


@shared_task(name="reprovision.sweep_deadlines")
@scoping.system_job(reason="background job: rebuild deadlines are swept on every site")
def sweep_rebuild_deadlines() -> int:
    from .jobs import sweep_deadlines

    return sweep_deadlines()


@shared_task(name="reprovision.fetch_image")
@scoping.system_job(reason="background job: downloading an OS image, which belongs to no site")
def fetch_image(image_id: str) -> None:
    from .fetcher import download_and_import

    download_and_import(image_id)
