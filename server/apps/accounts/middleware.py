from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect

_BYPASS_PREFIXES = (
    "/static/", "/api/", "/admin/", "/setup", "/login", "/logout", "/media/", "/agent/",
)


class SetupRedirectMiddleware:
    """Redirect to /setup/ when no admin account exists yet.

    Uses a per-process flag so the User table is only queried once — after
    setup completes the flag is set to True and the check never runs again.
    Gunicorn workers each carry their own flag; the check is cheap and only
    hits the DB once per worker lifetime.
    """

    _setup_complete = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not SetupRedirectMiddleware._setup_complete:
            path = request.path_info
            if not any(path.startswith(p) for p in _BYPASS_PREFIXES):
                if not get_user_model().objects.exists():
                    return HttpResponseRedirect("/setup/")
                SetupRedirectMiddleware._setup_complete = True
        return self.get_response(request)
