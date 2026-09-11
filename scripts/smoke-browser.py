#!/usr/bin/env python3
"""Browser smoke pass — clicks the app under the real CSP.

Why this exists: the 2026.12.0 sweep removed 'unsafe-inline' from script-src
and converted ~138 inline handlers to data-act dispatch. The unit tests prove
every data-act name resolves to a defined function. They do not prove the page
works, because they never render one. This found three inline <script> blocks
the browser was refusing to run while every test passed — including the one
that writes the agent install command onto the Settings page.

Run it against a server started the way production starts, not with DEBUG on:

    cd server && USE_SQLITE=1 DJANGO_DEBUG=false DJANGO_SECRET_KEY=test \
      VIGIL_PUBLIC_URL=http://127.0.0.1:8010 \
      VIGIL_SIGNING_KEY_SEED=<seed> \
      .venv/bin/python manage.py runserver 127.0.0.1:8010 --insecure --noreload

    .venv/bin/python ../scripts/smoke-browser.py /tmp/smoke

DEBUG=false matters twice: it turns on the cached template loader (so a
template edit needs a restart — a fix can look like it did not take), and it
is the only way the real CSP header is exercised.

Needs `playwright` and a chromium install (`playwright install chromium`).
Exits non-zero if anything was found. Screenshots land in the output dir.
"""
import json, sys, time
from collections import defaultdict
from playwright.sync_api import sync_playwright

import os

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8010")
USER = os.environ.get("SMOKE_USER", "smoke")
PASS = os.environ.get("SMOKE_PASS", "")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/smoke"

if not PASS:
    sys.exit(
        "Set SMOKE_PASS (and SMOKE_USER) to a staff account on this instance.\n"
        "Create one with:\n"
        "  manage.py shell -c \"from django.contrib.auth import get_user_model as g;"
        "u=g().objects.create(username='smoke',is_staff=True,is_superuser=True);"
        "u.set_password('...');u.save()\""
    )
os.makedirs(OUT, exist_ok=True)

findings = defaultdict(list)
current = {"page": "boot"}

# Capture CSP violations from inside the page: console text is lossy,
# the securitypolicyviolation event carries the blocked directive.
INIT = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', e => {
  window.__cspViolations.push({
    directive: e.violatedDirective,
    blocked: (e.blockedURI || '').slice(0, 200),
    sample: (e.sample || '').slice(0, 200),
    line: e.lineNumber,
    source: (e.sourceFile || '').slice(0, 200),
  });
});
"""

def record(kind, text):
    findings[current["page"]].append({"kind": kind, "text": str(text)[:500]})

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.add_init_script(INIT)
    page = ctx.new_page()

    page.on("console", lambda m: record("console:" + m.type, m.text)
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: record("pageerror", e))
    page.on("requestfailed", lambda r: record(
        "requestfailed", f"{r.method} {r.url} :: {r.failure}"))
    page.on("response", lambda r: record(
        "http", f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None)

    # ── login ────────────────────────────────────────────────────────
    current["page"] = "login"
    page.goto(BASE + "/login/", wait_until="networkidle")
    page.fill("input[name=username]", USER)
    page.fill("input[name=password]", PASS)
    page.click("button[type=submit], input[type=submit]")
    current["page"] = "dashboard(initial)"
    page.wait_for_load_state("networkidle")
    if "/login" in page.url:
        print("LOGIN FAILED, still at", page.url)
        print(page.inner_text("body")[:800])
        sys.exit(2)
    print("logged in ->", page.url)

    # ── enumerate the sidebar ────────────────────────────────────────
    nav = page.eval_on_selector_all(
        ".sidebar-icon[data-page]",
        "els => els.map(e => e.dataset.page)")
    print("sidebar pages:", nav)

    def drain(label):
        v = page.evaluate("window.__cspViolations || []")
        for item in v:
            record("CSP", json.dumps(item))
        page.evaluate("window.__cspViolations = []")
        page.screenshot(path=f"{OUT}/{label}.png", full_page=False)

    page.wait_for_timeout(2500)
    drain("00-dashboard")

    for name in nav:
        current["page"] = name
        page.click(f'.sidebar-icon[data-page="{name}"]')
        page.wait_for_timeout(2200)
        drain(f"nav-{name}")

    # ── tab bars within each page ────────────────────────────────────
    for name in nav:
        page.click(f'.sidebar-icon[data-page="{name}"]')
        page.wait_for_timeout(600)
        tabs = page.eval_on_selector_all(
            f'#page-{name} .tab-bar .tab',
            "els => els.map((e,i) => e.textContent.trim() || ('tab'+i))")
        for i, label in enumerate(tabs):
            current["page"] = f"{name}:tab:{label}"
            try:
                page.locator(f'#page-{name} .tab-bar .tab').nth(i).click()
                page.wait_for_timeout(1200)
                drain(f"tab-{name}-{i}")
            except Exception as e:
                record("clickfail", e)

    browser.close()

# ── report ───────────────────────────────────────────────────────────
print("\n" + "=" * 70)
total = 0
for pg, items in findings.items():
    real = [i for i in items if i["kind"] != "console:warning"]
    if not real:
        continue
    print(f"\n### {pg}")
    seen = set()
    for i in real:
        key = (i["kind"], i["text"])
        if key in seen:
            continue
        seen.add(key)
        total += 1
        print(f"  [{i['kind']}] {i['text']}")
print("\n" + "=" * 70)
print("TOTAL non-warning findings:", total)
with open(f"{OUT}/findings.json", "w") as f:
    json.dump(findings, f, indent=2, default=str)
print("screenshots + findings.json in", OUT)
sys.exit(1 if total else 0)
