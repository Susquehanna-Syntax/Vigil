"""Reference edition-extension app.

This is NOT a shipped feature. It's a working example of how a Pro/Enterprise
app plugs into core via the extension seams — copy this shape into the
Vigil-Pro / Vigil-Enterprise repos. It is never in INSTALLED_APPS by default;
load it only to validate the seams:

    VIGIL_EXTRA_APPS=apps.example_extension python manage.py runserver

See docs/pro-extension-points.md.
"""

default_app_config = "apps.example_extension.apps.ExampleExtensionConfig"
