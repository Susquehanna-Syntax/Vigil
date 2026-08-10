from pathlib import Path

from celery import shared_task


@shared_task(name="reprovision.import_iso")
def import_iso_task(image_id: str, iso_path: str) -> None:
    from .images import import_iso
    from .models import OSImage

    image = OSImage.objects.filter(pk=image_id).first()
    if image is None:
        return
    import_iso(image, Path(iso_path))


@shared_task(name="reprovision.sweep_deadlines")
def sweep_rebuild_deadlines() -> int:
    from .jobs import sweep_deadlines

    return sweep_deadlines()


@shared_task(name="reprovision.fetch_image")
def fetch_image(image_id: str) -> None:
    from .fetcher import download_and_import

    download_and_import(image_id)
