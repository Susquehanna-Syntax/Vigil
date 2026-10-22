import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vigil.settings")

django_app = get_asgi_application()

# Imported after Django is set up: the WebSocket routes touch models and settings.
from apps.remote.ws import websocket_app


async def application(scope, receive, send):
    """Django answers HTTP; live sessions answer their own WebSocket upgrade.

    Django's ASGI handler has no ``websocket`` path type, so an upgrade would
    reach a URL it cannot serve — the two are routed here, above it, and
    nothing else about the request pipeline changes.
    """
    if scope["type"] == "websocket":
        return await websocket_app(scope, receive, send)
    return await django_app(scope, receive, send)
