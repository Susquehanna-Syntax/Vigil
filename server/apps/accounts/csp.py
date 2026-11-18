"""A Content-Security-Policy header, staged honestly.

`base.html` carried a note for a while saying CSP was intended and would need a
server-side nonce. Phases 07a's three injection findings are all contained by
one, so it is worth having now rather than after the last rough edge is gone.

Styles carry no `'unsafe-inline'` either (S6 08m). The only style attribute
left in markup is the exact string `display:none;`, which scripts undo with
`element.style.display = ''`; `'unsafe-hashes'` plus that one hash allows it
and nothing else. A value worked out per render goes through `data-css` and
`applyDataCss` (vigil-utils.js), which set it through `element.style` - not
governed by `style-src` - from a short allowlist of properties.
`vigil/test_inline_styles.py` keeps both rules. The two inline `<style>` blocks
(status page, compliance report) carry the nonce, and base.html gives the
nonce to every `<style>` element a script creates, which is how xterm.js
themes a terminal.

What it does do is real: an injected `<script src>` pointing anywhere off-site
is blocked, `object-src 'none'` kills plugin vectors, `base-uri 'self'` stops a
`<base>` tag rewriting every relative URL on the page, `frame-ancestors 'none'`
stops clickjacking, and `form-action 'self'` stops an injected form posting
credentials elsewhere.

For scripts there is no `'unsafe-inline'` to remove: the inline handlers are
gone (`apps/hosts/test_inline_handlers.py` keeps them gone) and the inline
blocks in `base.html` carry a per-request nonce.
"""

import secrets

from django.conf import settings

#: sha256 of the only inline style attribute value the markup may carry,
#: `display:none;` (see vigil/test_inline_styles.py).
DISPLAY_NONE_HASH = "sha256-0EZqoz+oBhx7gF4nvY2bSqoGyy4zLjNF+SDQXGp/ZrY="

#: Sources the app legitimately loads from. Vigil vendors its JavaScript and
#: CSS rather than using a CDN, so 'self' covers everything except the data:
#: URIs the OS logos and favicons use.
#:
#: script-src carries no 'unsafe-inline'. Every inline handler in the templates
#: and in generated markup is gone — apps/hosts/test_inline_handlers.py fails
#: the build if one comes back — and the two genuinely inline <script> blocks
#: in base.html carry a per-request nonce instead. That is the difference
#: between a policy that documents an intention and one that stops an injected
#: <script> from running.
#:
#: style-src: this sheet, nonced <style> blocks, and the one inline attribute
#: the markup still writes - display:none; - by its hash. An injected style
#: attribute or <style> element is refused, so injected markup cannot restyle
#: the page into a convincing fake.
_POLICY_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'nonce-{nonce}' 'unsafe-hashes' '" + DISPLAY_NONE_HASH + "'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


#: Browser features Vigil never uses, switched off so an injected script or
#: frame cannot ask for them. WebAuthn stays on for this origin: hardware keys
#: sign in and confirm actions with it.
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), "
    "bluetooth=(), hid=(), midi=(), magnetometer=(), gyroscope=(), accelerometer=(), "
    "publickey-credentials-get=(self), publickey-credentials-create=(self)"
)


class ContentSecurityPolicyMiddleware:
    """Adds the policy to every response that does not already carry one.

    Never overwrites an existing header: an operator fronting Vigil with a
    proxy that sets its own, stricter policy should win.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Minted before the view runs so a template can render it into the two
        # inline blocks that legitimately need one.
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            policy = getattr(settings, "VIGIL_CSP", None)
            response["Content-Security-Policy"] = (
                policy or _POLICY_TEMPLATE.format(nonce=request.csp_nonce))
        # Same rule as the policy: a proxy's own header wins.
        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = PERMISSIONS_POLICY
        # Another site's page may not load Vigil's responses as resources.
        if "Cross-Origin-Resource-Policy" not in response:
            response["Cross-Origin-Resource-Policy"] = "same-origin"
        return response
