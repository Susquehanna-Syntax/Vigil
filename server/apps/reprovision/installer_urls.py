"""Unauthenticated installer routes.

Kept apart from urls.py so the auth posture of this file is impossible to
miss: nothing here is behind a session, because the installer that fetches
it has no credentials. See docs/reprovisioning.md §4.2.
"""
from django.urls import path, re_path

from . import installer_views

urlpatterns = [
    re_path(r"^answer/(?P<token>[\w\-]+)/?(?P<filename>[\w\-.]*)$",
            installer_views.answer_file, name="reprovision-answer"),
    re_path(r"^tree/(?P<image_id>[0-9a-fA-F\-]+)/(?P<path>.*)$",
            installer_views.install_tree, name="reprovision-tree"),
]

#: Mounted under api/v1/ by vigil/urls.py — the agent posts here, so it
#: belongs with the API even though it is unauthenticated.
enroll_urlpatterns = [
    path("reprovision/enroll", installer_views.enroll,
         name="reprovision-enroll"),
]
