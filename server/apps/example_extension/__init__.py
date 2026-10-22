"""Reference Business-extension app.

This is NOT a shipped feature. It's a working example of how a Business app
plugs into core via the extension seams — the Business apps (the
``vigil-business`` package) follow this shape. It is never in INSTALLED_APPS by default; load it
only to validate the seams:

    VIGIL_EXTRA_APPS=apps.example_extension python manage.py runserver

See docs/pro-extension-points.md.
"""

default_app_config = "apps.example_extension.apps.ExampleExtensionConfig"
