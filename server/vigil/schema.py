"""OpenAPI descriptions of Vigil's own authentication schemes (sprint M5).

drf-spectacular knows DRF's built-in authenticators only; without these, every
view that lists one of Vigil's warns that its security cannot be described.
The classes are named by import path, so a core install without the Business
apps simply never matches the scoped-key one. Defining them registers them;
this module is loaded as DEFAULT_SCHEMA_CLASS the first time a schema is
built, before any authenticator is matched. Importing it at app start instead
would read DRF's settings before Django is ready.
"""
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.openapi import AutoSchema


class ScopedApiKeyScheme(OpenApiAuthenticationExtension):
    """``Authorization: Bearer vsk_...``: a Business scoped API key."""

    target_class = "apps_business.apikeys.auth.ScopedKeyAuthentication"
    name = "scopedApiKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "A scoped API key (vsk_...), minted in Settings, API & Webhooks.",
        }


class JackilIntegrationKeyScheme(OpenApiAuthenticationExtension):
    """``Authorization: Bearer vjk_...``: the Jackil integration key."""

    target_class = "apps.jackil.integration_auth.IntegrationKeyAuthentication"
    name = "jackilIntegrationKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "A Jackil integration key (vjk_...).",
        }



class VigilAutoSchema(AutoSchema):
    """Operation ids from the view function, not the URL.

    The URL-derived default gives a list and its detail route the same id
    (``hosts_retrieve`` twice) and spectacular falls back to numbering them,
    which reshuffles whenever a route is added: a generated client's method
    names would change under its users. ``<app>_<function>`` is stable, with
    the method appended when one view serves several.
    """

    def get_operation_id(self):
        view = type(self.view)
        parts = view.__module__.split(".")
        app = parts[1] if parts[0] in ("apps", "apps_business") and len(parts) > 1 else parts[0]
        op = f"{app}_{view.__name__}"
        methods = [m for m in self.view.allowed_methods if m not in ("OPTIONS", "HEAD")]
        if len(methods) > 1:
            op = f"{op}_{self.method.lower()}"
        return op


#: Routes that are the same view as another, documented one.
_LEGACY_EXACT = {
    # The pre-2026.9.1 URL, meaning the tasks kind: /tasks/community/{kind}/.
    "/api/v1/tasks/community/",
}


def drop_legacy_routes(endpoints, **kwargs):
    """Spectacular preprocessing hook: leave the legacy aliases out.

    /api/v1/baselines/ still answers for old clients but is the same view as
    /api/v1/playbooks/, and /api/v1/tasks/community/ the same as its ``{kind}``
    route; listing both would document one API twice and give every operation
    a duplicate id.
    """
    return [e for e in endpoints
            if not e[0].startswith("/api/v1/baselines/") and e[0] not in _LEGACY_EXACT]
