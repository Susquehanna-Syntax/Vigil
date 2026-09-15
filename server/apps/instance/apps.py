from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save


class InstanceSettingsConfig(AppConfig):
    name = "apps.instance"
    label = "instance"
    verbose_name = "Instance settings"

    def ready(self):
        from .models import InstanceSetting
        post_save.connect(_invalidate, sender=InstanceSetting,
                          dispatch_uid="instance.invalidate_save")
        post_delete.connect(_invalidate, sender=InstanceSetting,
                            dispatch_uid="instance.invalidate_delete")


def _invalidate(**_kwargs):
    """Any write to the table drops this process's cached snapshot, so a save
    is visible to the next read in the same process without waiting out the
    TTL. Other processes wait out the TTL."""
    from .config import invalidate
    invalidate()
