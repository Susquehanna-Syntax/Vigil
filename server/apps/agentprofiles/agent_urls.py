"""The agent-facing route. Mounted at /agent/enroll-config/ by vigil/urls.py."""
from django.urls import path

from .redeem_views import redeem

urlpatterns = [
    path("<str:token>/", redeem, name="agent-enroll-config"),
]
