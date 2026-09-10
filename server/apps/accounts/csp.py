"""A Content-Security-Policy header, staged honestly.

`base.html` has carried a note for a while saying CSP was intended and would
need a server-side nonce. Phases 07a's three injection findings are all
contained by one, so it is worth having now rather than after the inline
handlers are gone.

What this policy does NOT do, stated plainly: it keeps `'unsafe-inline'` for
scripts. Thirty inline `onclick` attributes remain in JavaScript-generated
markup — alerts, rollouts, tasks, tags, waves, community, devices — every one
built from a server-generated UUID, so none is injectable today, but a strict
`script-src 'self'` would break all of them at once. Shipping a policy that
breaks the product is not hardening, and neither is shipping one that claims
more than it delivers.

What it does do is real: an injected `<script src>` pointing anywhere off-site
is blocked, `object-src 'none'` kills plugin vectors, `base-uri 'self'` stops a
`<base>` tag rewriting every relative URL on the page, `frame-ancestors 'none'`
stops clickjacking, and `form-action 'self'` stops an injected form posting
credentials elsewhere.

Removing `'unsafe-inline'` is the follow-up: convert the remaining inline
handlers to delegated listeners the way the pin bar and host cards now are,
then drop it here and add a nonce to the one inline block in `base.html`.
"""

from __future__ import annotations

from django.conf import settings

#: Sources the app legitimately loads from. Vigil vendors its JavaScript and
#: CSS rather than using a CDN, so 'self' covers everything except the data:
#: URIs the OS logos and favicons use.
_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class ContentSecurityPolicyMiddleware:
    """Adds the policy to every response that does not already carry one.

    Never overwrites an existing header: an operator fronting Vigil with a
    proxy that sets its own, stricter policy should win.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = getattr(
                settings, "VIGIL_CSP", _POLICY)
        return response
