"""``vigil``: hosts, alerts, tasks and deploys from a terminal.

    vigil login --url https://vigil.example.com
    vigil hosts
    vigil alerts
    vigil alerts ack 3f2b --for 2h
    vigil tasks
    vigil deploy "Restart nginx" --host web-01 --wait
    vigil api GET /api/v1/hosts/inventory/

Every command takes ``--json`` for the server's own answer, unformatted. The
endpoints used are listed in ENDPOINTS, which the server's tests hold against
the OpenAPI schema, so a renamed route or field fails there and not on someone's
machine.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time

from . import __version__, store
from .client import ApiError, CliError, Client, SignedOut
from .output import ago, safe, table

#: (method, path template, response fields this CLI reads). The server test
#: ``vigil/test_cli_contract.py`` checks each against the OpenAPI schema.
ENDPOINTS = {
    "session": ("GET", "/api/v1/accounts/session/",
                ("idle_seconds_left", "max_seconds_left", "idle_seconds")),
    "heartbeat": ("POST", "/api/v1/accounts/session/activity/",
                  ("idle_seconds_left", "max_seconds_left")),
    "hosts": ("GET", "/api/v1/hosts/",
              ("id", "hostname", "os", "ip_address", "status", "mode", "tags",
               "agent_version", "reboot_required", "last_checkin", "site_name")),
    "alerts": ("GET", "/api/v1/alerts/",
               ("id", "host_hostname", "state", "severity", "message", "fired_at",
                "acknowledged_until")),
    "alert_ack": ("POST", "/api/v1/alerts/{alert_id}/acknowledge/", ("id", "state")),
    "definitions": ("GET", "/api/v1/tasks/definitions/",
                    ("id", "name", "description", "risk_level", "action_count",
                     "owner_username", "last_run")),
    "deploy": ("POST", "/api/v1/tasks/definitions/{definition_id}/deploy/",
               ("id", "state", "host_count", "step_count", "skipped")),
    "runs": ("GET", "/api/v1/tasks/runs/", ("results",)),
    "run": ("GET", "/api/v1/tasks/runs/{run_id}/",
            ("id", "name_snapshot", "state", "host_count", "tasks", "created_at",
             "finished_at")),
}

#: A run in one of these has finished; only "completed" is a clean exit.
RUN_DONE = ("completed", "failed", "partial", "not_applicable")
#: How often --wait asks, and for how long before giving up.
WAIT_EVERY_SECONDS = 3
WAIT_LIMIT_SECONDS = 1800
HEARTBEAT_EVERY_SECONDS = 60

_DURATION = re.compile(r"^(\d+)\s*([smhd]?)$")
_UNIT = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _path(name: str, **ids) -> str:
    return ENDPOINTS[name][1].format(**ids)


def parse_duration(text: str) -> int:
    """"90m", "2h", "1d", "45s" or a bare number of minutes, in seconds."""
    match = _DURATION.match(text.strip().lower())
    if not match or int(match.group(1)) <= 0:
        raise CliError(f"Not a duration: {text!r}. Use 30m, 2h, 1d or 45s.")
    return int(match.group(1)) * _UNIT[match.group(2)]


def pick(rows: list, wanted: str, *, name_key: str, what: str):
    """The one row *wanted* names: an exact name (any case), a full id, or the
    start of an id. Anything ambiguous is refused, never guessed."""
    needle = wanted.strip().lower()
    exact = [r for r in rows if str(r.get(name_key, "")).lower() == needle
             or str(r.get("id", "")).lower() == needle]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        ids = ", ".join(str(r["id"])[:8] for r in exact)
        raise CliError(f"{len(exact)} {what}s are named {wanted!r}. Use an id: {ids}")
    by_id = [r for r in rows if len(needle) >= 4 and str(r.get("id", "")).lower().startswith(needle)]
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        raise CliError(f"{wanted!r} is the start of {len(by_id)} {what} ids. Type more of it.")
    raise CliError(f"No {what} named {wanted!r}.")


# ── the session ──────────────────────────────────────────────────────────────


class Context:
    """One command's view of the world: arguments, streams, and a client."""

    def __init__(self, args, *, out=None, err=None, prompt=input, secret=getpass.getpass,
                 sleep=time.sleep):
        self.args = args
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.prompt = prompt
        self.secret = secret
        self.sleep = sleep
        self._client = None

    # Every line this program prints goes through one of these two, and both
    # make control characters visible (output.safe): much of what is printed
    # was written by an agent.
    def say(self, text: str = "") -> None:
        print(safe(str(text)), file=self.out)

    def note(self, text: str) -> None:
        print(safe(str(text)), file=self.err)

    def emit(self, data) -> None:
        self.say(json.dumps(data, indent=2, sort_keys=True))

    def client(self) -> Client:
        if self._client is not None:
            return self._client
        saved = store.load()
        api_key = os.environ.get("VIGIL_API_KEY", "").strip()
        url = self.args.url or os.environ.get("VIGIL_URL") or saved.get("url") or ""
        if not url:
            raise CliError("Not signed in. Run `vigil login --url https://your-vigil`.")
        cookies = {}
        if not api_key:
            # Cookies go only to the server that issued them.
            if saved.get("url") and _same_server(saved["url"], url):
                cookies = saved.get("cookies") or {}
            if not cookies.get("sessionid"):
                raise CliError("Not signed in. Run `vigil login`.")
        self._client = Client(url, cookies=cookies, api_key=api_key,
                              ca_cert=self.args.ca_cert or saved.get("ca_cert") or "",
                              allow_http=self.args.allow_http or bool(saved.get("allow_http")))
        if cookies:
            self._keep_alive(saved)
        return self._client

    def _keep_alive(self, saved: dict) -> None:
        """A typed command is a person using the session; say so at most once
        a minute, so a burst of commands is one extra request."""
        if time.time() - float(saved.get("heartbeat_at") or 0) < HEARTBEAT_EVERY_SECONDS:
            return
        self._client.heartbeat()  # SignedOut here is the right answer to give
        saved["heartbeat_at"] = time.time()
        saved["cookies"] = self._client.cookies
        store.save(saved)


def _same_server(saved_url: str, url: str) -> bool:
    from .client import normalise_url
    try:
        return normalise_url(saved_url, allow_http=True) == normalise_url(url, allow_http=True)
    except CliError:
        return False


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_login(ctx: Context) -> int:
    saved = store.load()
    url = ctx.args.url or os.environ.get("VIGIL_URL") or saved.get("url") or ""
    if not url:
        url = ctx.prompt("Vigil address (https://...): ").strip()
    ca_cert = ctx.args.ca_cert or saved.get("ca_cert") or ""
    client = Client(url, ca_cert=ca_cert, allow_http=ctx.args.allow_http)
    username = ctx.args.user or ctx.prompt("User name: ").strip()
    client.begin_login(username, ctx.secret("Password: "))
    step = client.second_step()
    if step == "enrol":
        raise CliError("This account has no second factor yet. Sign in once in a "
                       "browser to set one up, then run `vigil login` again.")
    if step == "key-only":
        raise CliError("This account signs in with a security key only, which a "
                       "terminal cannot tap. Add an authenticator code in Settings "
                       "(Account & Security), then run `vigil login` again.")
    client.finish_login(ctx.prompt("6-digit code: ").strip().replace(" ", ""))
    store.save({"url": client.url, "user": username, "cookies": client.cookies,
                "ca_cert": ca_cert, "allow_http": bool(ctx.args.allow_http),
                "heartbeat_at": time.time()})
    left = client.api(*ENDPOINTS["session"][:2])
    ctx.say(f"Signed in to {client.url} as {username}. "
            f"The session ends after {_span(left['idle_seconds'])} without a command, "
            f"or {_span(left['max_seconds_left'])} from now.")
    return 0


def cmd_logout(ctx: Context) -> int:
    saved = store.load()
    if saved.get("cookies", {}).get("sessionid"):
        try:
            Client(saved["url"], cookies=saved["cookies"], ca_cert=saved.get("ca_cert") or "",
                   allow_http=True).logout()
        except CliError as exc:
            ctx.note(f"The server was not told ({exc}); the saved session is removed anyway.")
    store.clear()
    ctx.say("Signed out.")
    return 0


def cmd_status(ctx: Context) -> int:
    client = ctx.client()
    if client.api_key:
        client.api(*ENDPOINTS["hosts"][:2])  # any call proves the key
        if ctx.args.json:
            ctx.emit({"url": client.url, "auth": "api_key"})
        else:
            ctx.say(f"{client.url}: the API key in VIGIL_API_KEY works.")
        return 0
    left = client.api(*ENDPOINTS["session"][:2])
    if ctx.args.json:
        ctx.emit({"url": client.url, "auth": "session",
                  "user": store.load().get("user", ""), **left})
    else:
        ctx.say(f"Signed in to {client.url} as {store.load().get('user', '?')}. "
                f"{_span(left['idle_seconds_left'])} of idle time left, "
                f"{_span(left['max_seconds_left'])} until the sign-in ends.")
    return 0


def _span(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds >= 7200:
        return f"{seconds // 3600} hours"
    if seconds >= 120:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def cmd_hosts(ctx: Context) -> int:
    hosts = ctx.client().api(*ENDPOINTS["hosts"][:2])
    args = ctx.args
    if getattr(args, "name", None):
        host = pick(hosts, args.name, name_key="hostname", what="host")
        if args.json:
            ctx.emit(host)
            return 0
        for label, value in (
                ("Host", host["hostname"]), ("Id", host["id"]), ("Status", host["status"]),
                ("Mode", host["mode"]), ("OS", host.get("os")), ("Kernel", host.get("kernel")),
                ("Address", host.get("ip_address")), ("Site", host.get("site_name")),
                ("Tags", ", ".join(host.get("tags") or [])),
                ("Agent", host.get("agent_version")),
                ("Last check-in", ago(host.get("last_checkin"))),
                ("Reboot needed", "yes" if host.get("reboot_required") else "no")):
            ctx.say(f"{label + ':':<15}{value or '-'}")
        return 0
    if args.status:
        hosts = [h for h in hosts if h.get("status") == args.status]
    for tag in args.tag or []:
        hosts = [h for h in hosts if tag.lower() in [t.lower() for t in h.get("tags") or []]]
    if args.site:
        hosts = [h for h in hosts if (h.get("site_name") or "").lower() == args.site.lower()]
    hosts.sort(key=lambda h: h.get("hostname", "").lower())
    if args.json:
        ctx.emit(hosts)
        return 0
    sites = any(h.get("site_name") for h in hosts)
    rows = [[h["hostname"], h["status"], h["mode"], h.get("os") or "-",
             h.get("ip_address") or "-", ago(h.get("last_checkin")),
             *([h.get("site_name") or "-"] if sites else []),
             ", ".join(h.get("tags") or [])] for h in hosts]
    ctx.say(table(["HOST", "STATUS", "MODE", "OS", "ADDRESS", "LAST SEEN",
                   *(["SITE"] if sites else []), "TAGS"], rows, empty="No hosts match."))
    return 0


def cmd_alerts(ctx: Context) -> int:
    alerts = ctx.client().api(*ENDPOINTS["alerts"][:2], params={"state": ctx.args.state})
    if ctx.args.json:
        ctx.emit(alerts)
        return 0
    rows = [[str(a["id"])[:8], a.get("severity") or "-", a.get("host_hostname") or "-",
             ago(a.get("fired_at")), a.get("message") or ""] for a in alerts]
    ctx.say(table(["ID", "SEVERITY", "HOST", "FIRED", "MESSAGE"], rows,
                  empty=f"No {ctx.args.state} alerts."))
    return 0


def cmd_alerts_ack(ctx: Context) -> int:
    client = ctx.client()
    firing = client.api(*ENDPOINTS["alerts"][:2], params={"state": "firing"})
    body = {"duration_seconds": parse_duration(ctx.args.duration)} if ctx.args.duration else {}
    # Resolve every id first: a typo in the third must not leave two acknowledged.
    chosen = [pick(firing, wanted, name_key="id", what="firing alert")
              for wanted in ctx.args.ids]
    done = [client.api("POST", _path("alert_ack", alert_id=a["id"]), body) for a in chosen]
    if ctx.args.json:
        ctx.emit(done)
        return 0
    for alert in done:
        until = alert.get("acknowledged_until")
        ctx.say(f"Acknowledged {str(alert['id'])[:8]} ({alert.get('host_hostname') or '-'})"
                + (f" until {until}" if until else ", until it resolves"))
    return 0


def cmd_tasks(ctx: Context) -> int:
    rows = ctx.client().api(*ENDPOINTS["definitions"][:2], params={"scope": ctx.args.scope})
    rows.sort(key=lambda d: d.get("name", "").lower())
    if ctx.args.json:
        ctx.emit(rows)
        return 0
    ctx.say(table(
        ["TASK", "RISK", "STEPS", "OWNER", "LAST RUN", "ID"],
        [[d["name"], d.get("risk_level") or "-", d.get("action_count", "-"),
          d.get("owner_username") or "-",
          ago((d.get("last_run") or {}).get("created_at")), str(d["id"])[:8]] for d in rows],
        empty="No tasks."))
    return 0


def cmd_runs(ctx: Context) -> int:
    client = ctx.client()
    if getattr(ctx.args, "run_id", None):
        return _show_run(ctx, client.api("GET", _path("run", run_id=_run_id(client, ctx.args.run_id))))
    page = client.api(*ENDPOINTS["runs"][:2])
    if ctx.args.json:
        ctx.emit(page)
        return 0
    ctx.say(table(
        ["RUN", "STATE", "HOSTS", "BY", "STARTED", "ID"],
        [[r.get("name_snapshot") or r.get("playbook_name") or r.get("automation_name") or "-",
          r["state"], r.get("host_count", "-"), r.get("requested_by_username") or r.get("source") or "-",
          ago(r.get("created_at")), str(r["id"])[:8]] for r in page.get("results", [])],
        empty="No runs yet."))
    return 0


def _run_id(client: Client, wanted: str) -> str:
    if len(wanted) >= 32:
        return wanted
    return pick(client.api(*ENDPOINTS["runs"][:2]).get("results", []), wanted,
                name_key="id", what="run")["id"]


def _show_run(ctx: Context, run: dict) -> int:
    if ctx.args.json:
        ctx.emit(run)
    else:
        ctx.say(f"{run.get('name_snapshot') or 'Run'}: {run['state']} "
                f"({run.get('host_count', 0)} hosts, started {ago(run.get('created_at'))})")
        ctx.say(table(
            ["HOST", "STEP", "ACTION", "STATE", "OUTPUT"],
            [[t.get("host_hostname") or "-", t.get("step_label") or t.get("step_order", ""),
              t.get("action") or "-", t.get("state") or "-",
              _first_line(t.get("result_output"))] for t in run.get("tasks", [])],
            empty="No steps."))
    return 0 if run["state"] in ("completed", "running", "not_applicable") else 1


def _first_line(text) -> str:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[0][:80] + (" ..." if len(lines) > 1 or len(lines[0]) > 80 else "")


def cmd_deploy(ctx: Context) -> int:
    args = ctx.args
    if not args.host and not args.tag:
        raise CliError("Say where: --host NAME (repeatable) or --tag TAG.")
    if args.host and args.tag:
        raise CliError("Use --host or --tag, not both: the server targets one or the other.")
    client = ctx.client()
    definition = pick(client.api(*ENDPOINTS["definitions"][:2], params={"scope": "all"}),
                      args.task, name_key="name", what="task")
    body: dict = {}
    targets = ""
    if args.host:
        hosts = client.api(*ENDPOINTS["hosts"][:2])
        chosen = [pick(hosts, name, name_key="hostname", what="host") for name in args.host]
        body["host_ids"] = [h["id"] for h in chosen]
        targets = ", ".join(h["hostname"] for h in chosen)
    else:
        body["tags"] = args.tag
        targets = "every online host tagged " + " or ".join(args.tag)
    inputs = {}
    for pair in args.input or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise CliError(f"--input takes name=value, not {pair!r}.")
        inputs[key.strip()] = value
    if inputs:
        body["inputs"] = inputs

    ctx.note(f"Deploy {definition['name']!r} (risk {definition.get('risk_level') or '?'}, "
             f"{definition.get('action_count', '?')} steps) to {targets}.")
    # The server asks every deploy for a fresh code, whoever is asking: an API
    # key acts as the person who made it and needs that person's code too.
    body["totp"] = ctx.prompt("6-digit code to confirm: ").strip().replace(" ", "")
    run = client.api("POST", _path("deploy", definition_id=definition["id"]), body)
    for skip in run.get("skipped") or []:
        ctx.note(f"Skipped {skip.get('hostname')}: its agent does not allow "
                 f"{', '.join(skip.get('actions') or [])}.")
    if not args.wait:
        if args.json:
            ctx.emit(run)
        else:
            ctx.say(f"Run {str(run['id'])[:8]} started on {run.get('host_count', 0)} hosts. "
                    f"Follow it with `vigil runs {str(run['id'])[:8]}`.")
        return 0
    deadline = time.time() + WAIT_LIMIT_SECONDS
    while run["state"] not in RUN_DONE:
        if time.time() >= deadline:
            raise CliError(f"Run {run['id']} is still {run['state']} after "
                           f"{WAIT_LIMIT_SECONDS // 60} minutes. It carries on; "
                           f"check it with `vigil runs {str(run['id'])[:8]}`.")
        ctx.sleep(WAIT_EVERY_SECONDS)
        run = client.api("GET", _path("run", run_id=run["id"]))
    return _show_run(ctx, run)


def cmd_api(ctx: Context) -> int:
    body = None
    if ctx.args.data is not None:
        raw = sys.stdin.read() if ctx.args.data == "-" else ctx.args.data
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise CliError(f"--data is not JSON: {exc}") from exc
    result = ctx.client().api(ctx.args.method.upper(), ctx.args.path, body)
    if isinstance(result, str):
        ctx.say(result)
    elif result is not None:
        ctx.emit(result)
    return 0


# ── the parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS, help="the Vigil address (default: the one you signed in to, "
                                      "or VIGIL_URL)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the server's JSON")
    common.add_argument("--ca-cert", metavar="FILE", default=argparse.SUPPRESS,
                        help="a CA certificate to trust, for a Vigil behind your own CA")
    common.add_argument("--allow-http", action="store_true", default=argparse.SUPPRESS,
                        help="accept a plain-HTTP address that is not this machine")

    parser = argparse.ArgumentParser(
        prog="vigil", parents=[common],
        description="Vigil from a terminal: hosts, alerts, tasks and deploys.",
        epilog="Sign in with `vigil login`. For scripts, a Business scoped API key in "
               "VIGIL_API_KEY (with VIGIL_URL) needs no sign-in.")
    parser.add_argument("--version", action="version", version=f"vigil {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name, func, help_text, **kwargs):
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text, **kwargs)
        p.set_defaults(func=func)
        return p

    p = add("login", cmd_login, "Sign in with your password and authenticator code.")
    p.add_argument("--user", help="user name (asked for when left out)")
    add("logout", cmd_logout, "End the session and forget it.")
    add("status", cmd_status, "Show where you are signed in and for how much longer.")

    p = add("hosts", cmd_hosts, "List hosts, or show one.")
    p.add_argument("name", nargs="?", help="a host name or id, to show just that host")
    p.add_argument("--status", choices=["online", "offline", "pending", "rejected"])
    p.add_argument("--tag", action="append", help="only hosts with this tag (repeatable: all of them)")
    p.add_argument("--site", help="only hosts of this site")

    p = add("alerts", cmd_alerts, "List alerts, or acknowledge some.")
    p.add_argument("--state", default="firing", choices=["firing", "acknowledged", "resolved"])
    alert_sub = p.add_subparsers(dest="alert_command", metavar="ack")
    ack = alert_sub.add_parser("ack", parents=[common], help="Acknowledge firing alerts.",
                               description="Acknowledge firing alerts by id (or the start of one).")
    ack.add_argument("ids", nargs="+", metavar="id")
    ack.add_argument("--for", dest="duration", metavar="TIME",
                     help="for how long, such as 30m, 2h or 1d (default: until it resolves)")
    ack.set_defaults(func=cmd_alerts_ack)

    p = add("tasks", cmd_tasks, "List task definitions.")
    p.add_argument("--scope", default="all", choices=["mine", "all"])

    p = add("runs", cmd_runs, "List recent runs, or show one with each host's result.")
    p.add_argument("run_id", nargs="?", metavar="id")

    p = add("deploy", cmd_deploy, "Run a task on hosts. Asks for a fresh authenticator code.")
    p.add_argument("task", help="a task's name or id")
    p.add_argument("--host", action="append", help="a host name or id (repeatable)")
    p.add_argument("--tag", action="append", help="every online host with this tag (repeatable: any of them)")
    p.add_argument("--input", action="append", metavar="NAME=VALUE", help="a task input (repeatable)")
    p.add_argument("--wait", action="store_true",
                   help="wait for the run and exit non-zero unless it completed")

    p = add("api", cmd_api, "Call any API path and print the JSON. The paths are at /api/docs/.")
    p.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE",
                                      "get", "post", "put", "patch", "delete"])
    p.add_argument("path", help="such as /api/v1/hosts/")
    p.add_argument("--data", "-d", metavar="JSON", help="a JSON body, or - to read it from stdin")
    return parser


def main(argv=None, **streams) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # SUPPRESS above, defaults here: otherwise a subcommand's default would
    # overwrite `vigil --json hosts` with False.
    for name, default in (("url", None), ("json", False), ("ca_cert", None),
                          ("allow_http", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not getattr(args, "func", None):
        parser.print_help(streams.get("out") or sys.stdout)
        return 0
    ctx = Context(args, **streams)
    try:
        return args.func(ctx)
    except SignedOut as exc:
        if not os.environ.get("VIGIL_API_KEY"):
            store.save({k: v for k, v in store.load().items() if k != "cookies"})
        ctx.note(f"vigil: {exc}")
        return 3
    except ApiError as exc:
        ctx.note(f"vigil: {exc}")
        return 1
    except CliError as exc:
        ctx.note(f"vigil: {exc}")
        return 2
    except KeyboardInterrupt:
        ctx.note("")
        return 130
    except BrokenPipeError:  # `vigil hosts | head`
        return 0
