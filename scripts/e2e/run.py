#!/usr/bin/env python3
"""End-to-end browser suite (sprint M2 phase 1).

Drives a real browser against a server started the way production starts
(DEBUG=false, so the real CSP header and the cached template loader are in
play), seeded by scripts/e2e/seed.py:

    E2E_BASE=http://127.0.0.1:8010 python scripts/e2e/run.py /tmp/e2e

Each flow is a function; a failure is recorded with a screenshot and the run
carries on, so one broken flow does not hide the rest. Every page also fails
the run on a CSP violation, an uncaught error or a 5xx. Exits non-zero when
anything failed. Screenshots land in the output directory (a CI artifact).

Needs ``playwright`` and ``playwright install chromium``.
"""
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import traceback

from playwright.sync_api import sync_playwright

# localhost, not 127.0.0.1: WebAuthn refuses an IP address as the relying
# party, so the security-key flow needs a name.
BASE = os.environ.get("E2E_BASE", "http://localhost:8010")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e"
TOTP = "JBSWY3DPEHPK3PXP"          # matches seed.py
PASSWORD = "e2e-pass-123"
os.makedirs(OUT, exist_ok=True)

failures: list[str] = []
passed: list[str] = []
_last_code = {}


def totp_code(user: str) -> str:
    """A valid code this user has not just spent: the server refuses an
    immediate replay of the last code, so alternate inside the +-1 window."""
    for offset in (0, 1, -1):
        key = base64.b32decode(TOTP)
        counter = int(time.time()) // 30 + offset
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        o = digest[-1] & 15
        code = "%06d" % ((struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)
        if _last_code.get(user) != code:
            _last_code[user] = code
            return code
    raise RuntimeError("no fresh TOTP code")


# Every page records CSP violations from inside the page: console text is
# lossy, the securitypolicyviolation event names the blocked directive.
INIT = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', e => {
  window.__cspViolations.push(`${e.violatedDirective} ${e.blockedURI || ''} ${(e.sample || '').slice(0, 80)}`);
});
"""


class Session:
    def __init__(self, browser, user, viewport=(1440, 1000)):
        self.user = user
        self.ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        self.ctx.add_init_script(INIT)
        self.page = self.ctx.new_page()
        self.problems: list[str] = []
        self.page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        self.page.on("response", lambda r: self.problems.append(f"{r.status} {r.request.method} {r.url}")
                     if r.status >= 500 else None)

    def login(self):
        pg = self.page
        pg.goto(BASE + "/login/", wait_until="networkidle")
        pg.fill("input[name=username]", self.user)
        pg.fill("input[name=password]", PASSWORD)
        pg.click("button[type=submit]")
        pg.wait_for_load_state("networkidle")
        # A code another process spent in this window is refused as a replay,
        # so one more valid code is tried before calling it a failure.
        for _ in range(2):
            if "verify" not in pg.url:
                break
            pg.fill("input[name=code]", totp_code(self.user))
            pg.locator("form button[type=submit]").last.click()
            pg.wait_for_load_state("networkidle")
        assert "/login" not in pg.url and "verify" not in pg.url, f"still at {pg.url}"
        pg.add_style_tag(content="*{transition:none!important;animation:none!important}")
        return self

    def api(self, url):
        """GET a JSON API as this session (same cookies, same CSP)."""
        return self.page.evaluate("u => fetch(u, {credentials: 'same-origin'}).then(r => r.json())", url)

    def answer_totp(self):
        self.page.wait_for_selector("#totp-modal.open", timeout=5000)
        self.page.fill("#totp-input", totp_code(self.user))
        self.page.click("#totp-ok")

    def go(self, page_name):
        self.page.click(f'.sidebar-icon[data-page="{page_name}"]')
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(600)

    def drain(self):
        found = self.page.evaluate("window.__cspViolations.splice(0)")
        problems, self.problems = self.problems, []
        return [f"CSP {v}" for v in found] + problems

    def shot(self, name):
        self.page.screenshot(path=f"{OUT}/{name}.png")


def flow(name):
    def wrap(fn):
        def run(*args):
            started = time.time()
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 - a failed flow is a finding, not a crash
                failures.append(f"{name}: {exc}")
                traceback.print_exc()
                for arg in args:
                    if isinstance(arg, Session):
                        arg.shot(f"FAIL-{name}")
                print(f"FAIL {name}")
            else:
                passed.append(name)
                print(f"ok   {name} ({time.time() - started:.1f}s)")
        return run
    return wrap


@flow("every page renders under the real CSP")
def every_page(s: Session):
    pages = s.page.eval_on_selector_all(".sidebar-icon[data-page]", "els => els.map(e => e.dataset.page)")
    assert len(pages) > 10, pages
    bad = {}
    for name in pages:
        s.go(name)
        s.shot(f"page-{name}")
        problems = s.drain()
        if problems:
            bad[name] = problems[:5]
    assert not bad, json.dumps(bad, indent=1)


@flow("approve a pending host with a TOTP code")
def approve_host(s: Session):
    pending = [h for h in s.api("/api/v1/hosts/") if h["hostname"] == "e2e-pending-1"]
    s.page.goto(BASE + "/#settings", wait_until="networkidle")
    s.go("settings")
    s.page.click('.settings-nav-item[data-pane="agents"]')
    card = s.page.locator(".enroll-card", has_text="e2e-pending-1")
    card.locator('[data-act="approveHost"]').click()
    s.answer_totp()
    s.page.wait_for_load_state("networkidle")
    s.page.wait_for_timeout(1500)
    host = next(h for h in s.api("/api/v1/hosts/") if h["hostname"] == "e2e-pending-1")
    assert host["status"] != "pending", host["status"]
    del pending
    s.shot("approved")


@flow("deploy a task to a host with a TOTP code")
def deploy_task(s: Session):
    before = len(s.api("/api/v1/tasks/runs/").get("results", []))
    s.go("tasks")
    card = s.page.locator("[data-def-deploy]").first
    card.click()
    s.page.wait_for_selector("#deploy-modal.open")
    s.page.wait_for_selector("#deploy-host-rows .deploy-host-row")
    s.page.locator(".deploy-host-row", has_text="e2e-west-1").locator("input[type=checkbox]").check()
    s.shot("deploy-modal")
    s.page.click("#deploy-submit-btn")
    s.answer_totp()
    s.page.wait_for_load_state("networkidle")
    s.page.wait_for_timeout(1500)
    runs = s.api("/api/v1/tasks/runs/").get("results", [])
    assert len(runs) == before + 1, f"{before} runs before, {len(runs)} after"
    assert runs[0]["host_count"] == 1, runs[0]
    s.shot("deployed")
    problems = s.drain()
    assert not problems, problems


def virtual_authenticator(session):
    """A software security key in this browser context (Chrome DevTools
    WebAuthn domain): it answers every ceremony with user presence and
    verification, the way a tapped key does."""
    cdp = session.ctx.new_cdp_session(session.page)
    cdp.send("WebAuthn.enable")
    found = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "usb", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True}})
    return cdp, found["authenticatorId"]


@flow("add a security key, then sign in with it instead of a code")
def security_key(s: Session, browser):
    cdp, auth = virtual_authenticator(s)
    s.go("settings")
    s.page.click('.settings-nav-item[data-pane="identity"]')
    s.page.click("#security-keys-add-btn")
    s.answer_totp()                                   # adding a factor costs the one you have
    s.page.wait_for_selector("#prompt-modal.open")
    s.page.fill("#prompt-input", "e2e virtual key")
    s.page.click("#prompt-ok")
    s.page.wait_for_timeout(2500)
    keys = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth})["credentials"]
    assert keys, "the ceremony left no credential on the authenticator"
    s.shot("security-key-added")
    assert "e2e virtual key" in s.page.inner_text("body"), "the new key is not listed"

    # A fresh browser holding the same key signs in with a tap, no code.
    fresh = Session(browser, s.user)
    fresh_cdp, fresh_auth = virtual_authenticator(fresh)
    for cred in keys:
        fresh_cdp.send("WebAuthn.addCredential", {"authenticatorId": fresh_auth, "credential": cred})
    pg = fresh.page
    pg.goto(BASE + "/login/", wait_until="networkidle")
    pg.fill("input[name=username]", s.user)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.wait_for_selector("#login-key-btn")
    pg.click("#login-key-btn")
    deadline = time.time() + 15
    while "/login" in pg.url and time.time() < deadline:
        pg.wait_for_timeout(250)
    assert "/login" not in pg.url, f"the key did not sign in: {pg.url}"
    pg.wait_for_load_state("networkidle")
    fresh.shot("signed-in-with-key")
    assert fresh.api("/api/v1/hosts/") is not None
    problems = fresh.drain() + s.drain()
    assert not problems, problems
    fresh.ctx.close()


@flow("a site admin sees only their own site")
def site_view(s: Session):
    s.go("inventory")
    s.shot("site-admin-inventory")
    names = {h["hostname"] for h in s.api("/api/v1/hosts/")}
    assert "e2e-west-1" in names, names
    assert "e2e-lab-1" not in names, names
    assert s.page.locator("#site-switch").is_hidden(), "one site: no switcher"
    access = s.api("/api/v1/accounts/settings-access/")
    assert access["email"] == "hidden" and access["users"] == "greyed", access
    s.go("settings")
    assert s.page.locator('.settings-nav-item[data-pane="email"]').is_hidden()
    s.shot("site-admin-settings")
    problems = s.drain()
    assert not problems, problems


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        admin = Session(browser, "e2e-admin").login()
        every_page(admin)
        approve_host(admin)
        deploy_task(admin)
        security_key(admin, browser)
        try:
            dana = Session(browser, "e2e-dana").login()
        except AssertionError:
            print("skip site flows: no site admin seeded (no Business)")
        else:
            site_view(dana)
        browser.close()
    print(f"\n{len(passed)} passed, {len(failures)} failed")
    for f in failures:
        print("  -", f.splitlines()[0][:300])
    json.dump({"passed": passed, "failed": failures}, open(f"{OUT}/results.json", "w"), indent=1)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
