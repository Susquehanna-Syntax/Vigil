"""The HTTP side of the CLI: one Vigil server, one signed-in session.

Two ways to be someone:

* **A sign-in**, the same one the browser does: the password form at
  ``/login/``, then the six-digit code at ``/login/verify/``. The session and
  CSRF cookies it earns are kept in a file only this user can read, and the
  session ends when the server says so (idle and maximum sign-in time are the
  instance's settings, exactly as for a browser tab).
* **A scoped API key** (Business), from ``VIGIL_API_KEY``. Nothing is stored.

Standard library only, so the CLI installs anywhere Python does. TLS is always
verified; ``ca_cert`` adds a private CA for an install behind its own.
"""
from __future__ import annotations

import html
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import CookieError, SimpleCookie

USER_AGENT = "vigil-cli"
TIMEOUT_SECONDS = 30
#: The cookies a session is made of. Nothing else the server sets is kept.
SESSION_COOKIES = ("sessionid", "csrftoken")
HEARTBEAT_PATH = "/api/v1/accounts/session/activity/"

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_AUTH_ERROR = re.compile(r'<div class="auth-error">(.*?)</div>', re.S)
_TAG = re.compile(r"<[^>]+>")


class CliError(Exception):
    """Something to tell the person at the terminal, and stop."""


class ApiError(CliError):
    """The server answered with an error status."""

    def __init__(self, status: int, message: str, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class SignedOut(ApiError):
    """No session, or one the server has ended."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The sign-in forms answer with a redirect, and where it points is the
    answer: following it would throw that away."""

    def redirect_request(self, *args, **kwargs):
        return None


def normalise_url(url: str, *, allow_http: bool = False) -> str:
    """``https://host[:port]`` with no path. Plain HTTP is for this machine
    only unless the caller insists: a password and a session would cross the
    network readable."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise CliError("No server address. Run `vigil login --url https://your-vigil`.")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CliError(f"Not a server address: {url}")
    if parts.username or parts.password:
        raise CliError("Leave the user name and password out of the address.")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK and not allow_http:
        raise CliError(
            f"{url} is plain HTTP: your password and session would cross the network "
            "unencrypted. Use https://, or pass --allow-http if you accept that.")
    return f"{parts.scheme}://{parts.netloc}"


def _error_text(body) -> str:
    """The sentence in an API error body, whichever key the view used."""
    if isinstance(body, dict):
        for key in ("error", "detail", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        errors = body.get("errors")
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {v}" for k, v in errors.items())
    if isinstance(body, str) and body.strip():
        return body.strip()[:300]
    return ""


def _form_error(page: str) -> str:
    found = _AUTH_ERROR.search(page or "")
    return html.unescape(_TAG.sub("", found.group(1))).strip() if found else ""


class Client:
    def __init__(self, url: str, *, cookies: dict | None = None, api_key: str = "",
                 ca_cert: str = "", allow_http: bool = False):
        self.url = normalise_url(url, allow_http=allow_http)
        self.cookies = {k: v for k, v in (cookies or {}).items() if k in SESSION_COOKIES}
        self.api_key = api_key
        handlers: list = [_NoRedirect()]
        if self.url.startswith("https://"):
            context = ssl.create_default_context(cafile=ca_cert or None)
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)

    # ── transport ────────────────────────────────────────────────────────────

    def _send(self, method: str, path: str, *, data: bytes | None = None,
              content_type: str = "", accept: str = "application/json"):
        """(status, headers, text). Never follows a redirect; never raises for
        a status, only for a server that cannot be reached."""
        if not path.startswith("/"):
            raise CliError(f"A path starts with /: {path}")
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if method not in ("GET", "HEAD"):
            # Django wants the token and, over HTTPS, a Referer from this site.
            headers["Referer"] = self.url + "/"
            if self.cookies.get("csrftoken"):
                headers["X-CSRFToken"] = self.cookies["csrftoken"]
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers=headers)
        try:
            response = self._opener.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:  # any 3xx/4xx/5xx: still an answer
            response = exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise CliError(
                    f"{self.url} presented a certificate this machine does not trust "
                    f"({reason.verify_message}). If Vigil is behind your own CA, pass "
                    "--ca-cert with that CA's file.") from exc
            raise CliError(f"Could not reach {self.url}: {reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CliError(f"Could not reach {self.url}: {exc}") from exc
        with response:
            text = response.read().decode("utf-8", "replace")
            self._keep_cookies(response.headers.get_all("Set-Cookie") or [])
            return response.status, response.headers, text

    def _keep_cookies(self, set_cookie_headers) -> None:
        if self.api_key:
            return
        for header in set_cookie_headers:
            try:
                jar = SimpleCookie(header)
            except CookieError:
                continue
            for name, morsel in jar.items():
                if name not in SESSION_COOKIES:
                    continue
                if morsel.value in ("", '""'):
                    self.cookies.pop(name, None)  # the server deleted it
                else:
                    self.cookies[name] = morsel.value

    # ── the API ──────────────────────────────────────────────────────────────

    def api(self, method: str, path: str, body=None, *, params: dict | None = None):
        """One JSON call. Returns the decoded body (None for an empty one)."""
        if params:
            query = urllib.parse.urlencode(
                [(k, v) for k, v in params.items() if v is not None], doseq=True)
            if query:
                path += ("&" if "?" in path else "?") + query
        data = json.dumps(body).encode() if body is not None else None
        status, headers, text = self._send(
            method.upper(), path, data=data,
            content_type="application/json" if data is not None else "")
        decoded = None
        if text.strip():
            try:
                decoded = json.loads(text)
            except ValueError:
                decoded = text
        if 200 <= status < 300:
            return decoded
        message = _error_text(decoded)
        if 300 <= status < 400:
            raise ApiError(status, f"{path} moved to {headers.get('Location', '?')}: "
                                   "check the path (most end with a slash).", decoded)
        # A session that is missing or ended, as opposed to a 401 that answers
        # this request (a wrong confirmation code is one): the middleware names
        # its reason, and DRF has one sentence for "nobody is signed in".
        reason = str(decoded.get("reason", "")) if isinstance(decoded, dict) else ""
        if status in (401, 403) and not self.api_key and (
                reason.startswith("session_")
                or "credentials were not provided" in message):
            raise SignedOut(status, (message + " " if reason else "") + "Run `vigil login`.",
                            decoded)
        raise ApiError(status, message or f"The server answered {status}.", decoded)

    def heartbeat(self):
        """Tell the server a person is using this session (a command typed at
        a terminal is real use, like a click). Returns the time left."""
        return self.api("POST", HEARTBEAT_PATH)

    # ── signing in ───────────────────────────────────────────────────────────

    def begin_login(self, username: str, password: str) -> None:
        """The password step. Raises with the form's own words when refused."""
        self.cookies.clear()
        status, _headers, _page = self._send("GET", "/login/", accept="text/html")
        if status == 302:
            raise CliError("This Vigil has not been set up yet: open it in a browser "
                           "to create the first account.")
        if status != 200 or not self.cookies.get("csrftoken"):
            raise CliError(f"{self.url} did not answer like a Vigil sign-in page "
                           f"(status {status}). Check the address.")
        form = urllib.parse.urlencode({
            "username": username, "password": password,
            "csrfmiddlewaretoken": self.cookies["csrftoken"]}).encode()
        status, headers, page = self._send(
            "POST", "/login/", data=form, accept="text/html",
            content_type="application/x-www-form-urlencoded")
        if status == 302 and "/login/verify" in (headers.get("Location") or ""):
            return
        if status == 403:
            raise CliError("The server refused the sign-in form (CSRF). If Vigil is "
                           "behind a proxy, sign in at the address in VIGIL_PUBLIC_URL.")
        raise CliError(_form_error(page) or "The sign-in was refused.")

    def second_step(self) -> str:
        """What the verify page asks for: "code", "key-only" (a security key
        and no authenticator: nothing a terminal can answer) or "enrol"."""
        status, _headers, page = self._send("GET", "/login/verify/", accept="text/html")
        if status != 200:
            raise CliError("The sign-in timed out. Start again.")
        if "auth-secret-value" in page:  # the page is showing a new TOTP secret
            return "enrol"
        return "code" if 'name="code"' in page else "key-only"

    def finish_login(self, code: str) -> None:
        """The second step. Raises with the page's own words on a wrong code."""
        form = urllib.parse.urlencode({
            "code": code, "csrfmiddlewaretoken": self.cookies.get("csrftoken", "")}).encode()
        status, headers, page = self._send(
            "POST", "/login/verify/", data=form, accept="text/html",
            content_type="application/x-www-form-urlencoded")
        if status == 302 and "/login" not in (headers.get("Location") or ""):
            return
        if status == 302:
            raise CliError("The sign-in timed out. Start again.")
        raise CliError(_form_error(page) or "That code was refused.")

    def logout(self) -> None:
        if self.cookies.get("sessionid"):
            self._send("POST", "/logout/", data=urllib.parse.urlencode({
                "csrfmiddlewaretoken": self.cookies.get("csrftoken", "")}).encode(),
                content_type="application/x-www-form-urlencoded", accept="text/html")
        self.cookies.clear()
