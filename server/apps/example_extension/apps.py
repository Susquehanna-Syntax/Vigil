from django.apps import AppConfig


class ExampleExtensionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.example_extension"
    label = "example_extension"
    verbose_name = "Example Edition Extension"

    def ready(self):
        # The single line every edition app needs: register features and
        # subscribe to hooks. Keep heavy logic out of ready() — delegate.
        from .registration import register_extension

        register_extension()
