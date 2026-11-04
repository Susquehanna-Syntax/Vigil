#!/usr/bin/env python3
"""A real agent against a real server, end to end (sprint M2 phase 2).

Starts the agent (from source, or the installed binary), then acts as an
operator through the server's own API, with a password and TOTP sign-in:
waits for the host to register, approves it, deploys the seeded task to it,
and waits for the agent to run it and report back. The agent verifies the
task's signature before running it, so a "completed" result proves the
signing path too.

    python scripts/agent-e2e.py --server http://127.0.0.1:8010 \
        --agent "python -m vigil_agent" --workdir /tmp/agent-e2e

The server must be seeded by scripts/e2e/seed.py. Exits non-zero, with the
agent's log, when any step fails or times out.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import struct
import subprocess
import sys
import time
from pathlib import Path

import requests

TOTP = "JBSWY3DPEHPK3PXP"          # matches scripts/e2e/seed.py
USER, PASSWORD = "e2e-admin", "e2e-pass-123"
TASK = "e2e check nginx"
_spent = set()


def code() -> str:
    for offset in (0, 1, -1):
        counter = int(time.time()) // 30 + offset
        digest = hmac.new(base64.b32decode(TOTP), struct.pack(">Q", counter), hashlib.sha1).digest()
        o = digest[-1] & 15
        value = "%06d" % ((struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)
        if value not in _spent:
            _spent.add(value)
            return value
    raise RuntimeError("no unspent TOTP code; wait for the next window")


class Operator:
    def __init__(self, base):
        self.base = base
        self.s = requests.Session()

    def _csrf(self):
        return self.s.cookies.get("csrftoken", "")

    def login(self):
        self.s.get(f"{self.base}/login/")
        r = self.s.post(f"{self.base}/login/", data={
            "username": USER, "password": PASSWORD, "csrfmiddlewaretoken": self._csrf()},
            headers={"Referer": f"{self.base}/login/"})
        if "code" in r.text and "verify" in r.url:
            r = self.s.post(r.url, data={"code": code(), "csrfmiddlewaretoken": self._csrf()},
                            headers={"Referer": r.url})
        me = self.s.get(f"{self.base}/api/v1/hosts/")
        assert me.status_code == 200, f"sign-in failed: {me.status_code}"

    def get(self, path):
        r = self.s.get(f"{self.base}{path}")
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = self.s.post(f"{self.base}{path}", data=json.dumps(body),
                        headers={"Content-Type": "application/json", "X-CSRFToken": self._csrf(),
                                 "Referer": f"{self.base}/"})
        assert r.status_code < 300, f"POST {path}: {r.status_code} {r.text[:300]}"
        return r.json() if r.content else {}


def wait(what, fn, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {what}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--agent", required=True, help="command that runs the agent; -c <config> is appended")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    work = Path(args.workdir)
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "scripts").mkdir(exist_ok=True)
    # A tag no other host carries identifies this run's agent, whatever the
    # machine is called and whatever earlier runs left behind.
    marker = f"e2e-run-{secrets.token_hex(4)}"
    config = work / "agent.yml"
    config.write_text(
        f"server_url: {args.server}\n"
        f"agent_token: e2e-{secrets.token_hex(16)}\n"
        "checkin_interval: 10\n"
        "mode: managed\n"
        f"data_dir: {json.dumps(str(work / 'data'))}\n"
        f"scripts_dir: {json.dumps(str(work / 'scripts'))}\n"
        "allowlist:\n  - check_service\n"
        f"tags:\n  - e2e-agent\n  - {marker}\n")
    config.chmod(0o600)   # the agent warns about a config others can read
    log = open(work / "agent.log", "w")
    agent = subprocess.Popen(shlex.split(args.agent) + ["-c", str(config)], stdout=log,
                             stderr=subprocess.STDOUT)
    op = Operator(args.server)
    try:
        op.login()
        print("signed in as", USER)

        def registered():
            # Agent-advertised tags arrive namespaced, so an agent cannot pose
            # as an operator-set tag.
            return next((h for h in op.get("/api/v1/hosts/") if f"agent:{marker}" in (h.get("tags") or [])), None)
        host = wait("the agent to register", registered, 60)
        print("registered:", host["hostname"], host["status"])

        op.post(f"/api/v1/hosts/{host['id']}/approve/", {"totp": code()})
        print("approved")
        # Approval can fold a re-enrolled machine into its earlier row, so the
        # host is found again rather than trusted by the id it registered as;
        # and the deploy waits for a check-in after approval, which is when
        # the agent reports its mode and its allowlist.
        approved = registered() or {}
        before = approved.get("last_checkin")

        def checked_in_since():
            now = registered() or {}
            return (now if now.get("last_checkin") and now.get("last_checkin") != before
                    and now.get("mode") == "managed" and now.get("status") == "online" else None)
        host = wait("a managed-mode check-in after approval", checked_in_since, 60)
        print("checked in after approval, managed")

        definitions = op.get("/api/v1/tasks/definitions/")
        definitions = definitions.get("results", definitions) if isinstance(definitions, dict) else definitions
        definition = next(d for d in definitions if d["name"] == TASK)
        run = op.post(f"/api/v1/tasks/definitions/{definition['id']}/deploy/",
                      {"host_ids": [host["id"]], "totp": code()})
        run_id = run.get("run_id") or run.get("id")
        print("deployed, run", run_id)

        def reported():
            detail = op.get(f"/api/v1/tasks/runs/{run_id}/")
            tasks = detail.get("tasks") or []
            done = [t for t in tasks if t.get("state") in ("completed", "failed", "skipped")]
            return detail if tasks and len(done) == len(tasks) else None
        detail = wait("the agent to run the task and report", reported, args.timeout)
        states = [t.get("state") for t in detail["tasks"]]
        print("reported:", states)
        assert all(s in ("completed", "failed") for s in states), states
        rejected = [t for t in detail["tasks"] if re.search(r"signature", json.dumps(t), re.I)
                    and t.get("state") == "failed"]
        assert not rejected, f"the agent refused the signature: {rejected}"
        print("agent end-to-end: ok")
        return 0
    except Exception as exc:  # noqa: BLE001 - print the agent's side before failing
        print("FAILED:", exc)
        log.flush()
        print("---- agent log ----")
        print((work / "agent.log").read_text()[-6000:])
        return 1
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.kill()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
