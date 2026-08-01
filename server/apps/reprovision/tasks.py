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
