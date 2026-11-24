"""Seed a throwaway instance with an invented fleet, for screenshots and demos.

    cd server && USE_SQLITE=1 ... python manage.py shell < ../scripts/demo-seed.py

Run it on an empty database (after migrate). Everything here is made up: the
hostnames, addresses, tickets of work and findings belong to no real network.
Hosts arrive through the agent API (register, then check-ins with six hours of
metric history), and playbooks, status pages and reports through the same API
the dashboard uses, so every page shows data in the shape it really gets.

Sign in as  demo / demo-pass-123,  TOTP secret JBSWY3DPEHPK3PXP.
"""
import json
import random
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils.timezone import now

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert
from apps.hosts.models import Host
from apps.playbooks.models import Playbook
from apps.policies.models import AppRule, UpdatePolicy
from apps.relays.models import Relay
from apps.statuspage.models import HostUptimeSample, StatusPage
from apps.tasks.models import Task, TaskDefinition, TaskRun
from vigil import scoping

TOTP = "JBSWY3DPEHPK3PXP"
PASSWORD = "demo-pass-123"
rng = random.Random(2026)
NOW = now()

# hostname, os, kernel, ip, tags, mode, (cpu, mem, disk) baselines, online
FLEET = [
    ("web-01", "Ubuntu 24.04 LTS", "6.8.0-48-generic", "10.20.1.11", ["prod", "web"], "managed", (34, 58, 41), True),
    ("web-02", "Ubuntu 24.04 LTS", "6.8.0-48-generic", "10.20.1.12", ["prod", "web"], "managed", (29, 55, 39), True),
    ("db-01", "Debian 12", "6.1.0-26-amd64", "10.20.2.21", ["prod", "database"], "monitor", (49, 76, 72), True),
    ("db-02", "Debian 12", "6.1.0-26-amd64", "10.20.2.22", ["prod", "database"], "monitor", (37, 69, 66), True),
    ("docker-01", "Ubuntu 22.04 LTS", "5.15.0-122-generic", "10.20.3.31", ["prod", "containers"], "full_control", (61, 71, 58), True),
    ("backup-01", "Rocky Linux 9", "5.14.0-427.el9", "10.20.4.41", ["infra", "storage"], "monitor", (12, 35, 91), True),
    ("dc-01", "Windows Server 2022", "10.0.20348", "10.20.5.51", ["infra", "windows"], "managed", (18, 47, 52), True),
    ("dc-02", "Windows Server 2022", "10.0.20348", "10.20.5.52", ["infra", "windows"], "managed", (15, 44, 49), True),
    ("ci-runner-01", "Ubuntu 24.04 LTS", "6.8.0-48-generic", "10.20.6.71", ["ci"], "managed", (72, 63, 47), True),
    ("ci-runner-02", "Ubuntu 24.04 LTS", "6.8.0-48-generic", "10.20.6.72", ["ci"], "managed", (0, 0, 0), False),
    ("edge-gw-01", "Debian 12", "6.1.0-26-amd64", "10.20.0.1", ["edge", "network"], "monitor", (9, 28, 22), True),
    ("mon-01", "Alpine Linux 3.20", "6.6.58-0-lts", "10.20.0.9", ["infra"], "monitor", (14, 31, 26), True),
    ("files-01", "Windows Server 2019", "10.0.17763", "10.20.4.45", ["infra", "storage", "windows"], "managed", (21, 52, 83), True),
    ("lab-mac-01", "macOS 15.1", "24.1.0", "10.20.8.81", ["lab"], "managed", (26, 61, 44), True),
]


# manufacturer, model, CPU, cores, RAM in GB
HARDWARE = {
    "_": ("Dell Inc.", "PowerEdge R650", "Intel Xeon Silver 4314", 16, 64),
    "backup-01": ("Supermicro", "SYS-5019C-M", "Intel Xeon E-2224", 4, 32),
    "dc-01": ("Dell Inc.", "PowerEdge R350", "Intel Xeon E-2336", 6, 32),
    "dc-02": ("Dell Inc.", "PowerEdge R350", "Intel Xeon E-2336", 6, 32),
    "ci-runner-01": ("HP", "ProDesk 600 G6", "Intel Core i7-10700", 8, 32),
    "ci-runner-02": ("HP", "ProDesk 600 G6", "Intel Core i7-10700", 8, 32),
    "edge-gw-01": ("Protectli", "VP2420", "Intel Celeron J6412", 4, 8),
    "mon-01": ("Raspberry Pi", "Pi 5 Model B", "Cortex-A76", 4, 8),
    "files-01": ("Lenovo", "ThinkSystem SR250", "Intel Xeon E-2378", 8, 64),
    "lab-mac-01": ("Apple", "Mac mini (M2, 2023)", "Apple M2", 8, 16),
    "docker-01": ("Dell Inc.", "PowerEdge R660", "Intel Xeon Gold 5416S", 16, 128),
}


def say(msg):
    print(f"demo-seed: {msg}")


def wobble(base, spread=9.0, lo=1.0, hi=99.0):
    return max(lo, min(hi, base + rng.uniform(-spread, spread)))


with scoping.system(reason="demo seed: building an invented fleet"):
    User = get_user_model()
    admin = User.objects.create_superuser("demo", "demo@example.invalid", PASSWORD,
                                          first_name="Dana", last_name="Reyes")
    profile, _ = UserProfile.objects.get_or_create(user=admin)
    profile.role = Role.ADMIN
    profile.totp_secret = TOTP
    profile.totp_confirmed_at = NOW
    profile.save()

    agent = Client(HTTP_HOST="localhost")
    hosts = {}
    for name, os_name, kernel, ip, tags, mode, (cpu, mem, disk), online in FLEET:
        token = secrets.token_hex(32)
        resp = agent.post("/api/v1/register", data=json.dumps({
            "agent_token": token, "hostname": name, "os": os_name, "kernel": kernel,
            "machine_id": secrets.token_hex(16), "mode": mode}),
            content_type="application/json", REMOTE_ADDR=ip)
        if resp.status_code not in (200, 201):
            say(f"register {name}: {resp.status_code} {resp.content[:200]}")
            continue
        host = Host.objects.get(hostname=name)
        host.status = Host.Status.ONLINE
        host.mode = mode
        host.tags = tags
        host.agent_version = "2026.17.0"
        host.agent_allow_remote_shell = mode in ("managed", "full_control")
        host.save()
        hosts[name] = host
        if not online:
            continue
        # Six hours of history, a point every ten minutes, oldest first.
        counters = {"recv": rng.uniform(1e10, 5e11), "sent": rng.uniform(1e10, 2e11)}
        # The last two a minute apart: the cards read a rate from the last
        # five minutes, so they need two readings inside it.
        for seconds in [600 * k for k in range(36, 0, -1)] + [60, 0]:
            at = (NOW - timedelta(seconds=seconds)).isoformat()
            points = []

            def add(category, metric, value, labels=None):
                points.append({"category": category, "metric": metric, "value": round(value, 2),
                               "labels": labels or {}, "time": at})

            add("cpu", "usage_percent", wobble(cpu), {"core": "total"})
            add("memory", "usage_percent", wobble(mem, 4))
            add("disk", "usage_percent", wobble(disk, 1.5), {"mount": "/"})
            # Running byte counters, as the agent sends them: the cards work out
            # the rate from the last two points.
            gap = 60 if seconds <= 60 else 600
            counters["recv"] += wobble(2.2e6, 1.4e6, 4e4, 9e6) * gap
            counters["sent"] += wobble(8.0e5, 5e5, 2e4, 4e6) * gap
            add("network", "bytes_recv", counters["recv"], {"interface": "eth0"})
            add("network", "bytes_sent", counters["sent"], {"interface": "eth0"})
            body = {"hostname": name, "os": os_name, "kernel": kernel, "vigil_version": "2026.17.0",
                    "mode": mode, "features": ["signed_v2"], "metrics": points}
            if seconds == 0:
                # The hardware snapshot, once, with the newest check-in.
                maker, model, cpu_model, cores, ram = HARDWARE.get(name, HARDWARE["_"])
                body["inventory"] = {
                    "manufacturer": maker, "model_name": model, "cpu_model": cpu_model,
                    "cpu_cores": cores, "ram_total_bytes": ram * 1024 ** 3,
                    "service_tag": secrets.token_hex(4).upper(),
                    "mac_addresses": ["52:54:00:%02x:%02x:%02x" % (rng.randrange(256), rng.randrange(256), rng.randrange(256))]}
            agent.post("/api/v1/checkin", data=json.dumps(body),
                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=ip)
    Host.objects.filter(hostname="ci-runner-02").update(
        status=Host.Status.OFFLINE, last_checkin=NOW - timedelta(hours=7))
    say(f"{len(hosts)} hosts")

    # Alerts: a disk filling up, a runner that went quiet, a busy database.
    for host, severity, message, value in (
        ("backup-01", "warning", "Disk usage on / is 91% (threshold 90%)", 91),
        ("ci-runner-02", "critical", "No check-in for 7 hours", None),
        ("db-01", "warning", "Memory usage is 86% (threshold 85%)", 86),
    ):
        Alert.objects.create(host=hosts[host], rule=None, state=Alert.State.FIRING,
                             severity=severity, message=message, metric_value=value)
    acked = Alert.objects.create(host=hosts["files-01"], rule=None, state=Alert.State.ACKNOWLEDGED,
                                 severity="warning", message="Disk usage on C: is 83% (threshold 80%)",
                                 metric_value=83)
    Alert.objects.filter(pk=acked.pk).update(fired_at=NOW - timedelta(hours=3))

    # A playbook with a branch: check, then patch or install, then verify.
    web = Client(HTTP_HOST="localhost")
    web.force_login(admin)
    defs = {d.name: d for d in TaskDefinition.objects.filter(owner=None)}

    def pick(*names):
        for n in names:
            for have, d in defs.items():
                if n.lower() in have.lower():
                    return d
        return next(iter(defs.values()))

    # A library of the user's own tasks, saved from the built-in ones.
    for name in ("Upgrade outdated apps", "Restart a service", "Check a service",
                 "Run a Trivy scan", "Refresh software inventory", "Find hosts listening on a port"):
        d = defs.get(name)
        if d is None:
            continue
        r = web.post("/api/v1/tasks/definitions/", data=json.dumps({"yaml_source": d.yaml_source}),
                     content_type="application/json")
        if r.status_code not in (200, 201):
            say(f"task {name}: {r.status_code} {r.content[:200]}")

    check = pick("Check a service")
    patch = pick("Upgrade outdated apps")
    install = pick("Start a service")
    verify = pick("Restart a service")
    resp = web.post("/api/v1/playbooks/", data=json.dumps({
        "name": "Patch the web tier", "description": "Check nginx: patch it if it is running, start it if not, then restart it.",
        "target_tags": ["web"], "flow_steps": [
            {"definition_id": str(check.id), "id": "check"},
            {"if": 'steps.check.status == "ok"',
             "then": [{"definition_id": str(patch.id), "id": "patch"}],
             "else": [{"definition_id": str(install.id), "id": "install"}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ]}), content_type="application/json")
    if resp.status_code not in (200, 201):
        say(f"playbook: {resp.status_code} {resp.content[:300]}")
    playbook = Playbook.objects.filter(name="Patch the web tier").first()

    # Thirty days of runs of that playbook, mostly green.
    if playbook:
        for day in range(30, 0, -1):
            for _ in range(rng.randint(1, 3)):
                started = NOW - timedelta(days=day, hours=rng.randint(0, 20))
                failed = rng.random() < 0.12
                run = TaskRun.objects.create(
                    playbook=playbook, name_snapshot=playbook.name, requested_by=admin,
                    source="playbook", host_count=2, step_count=3,
                    state="failed" if failed else "completed")
                TaskRun.objects.filter(pk=run.pk).update(
                    created_at=started, finished_at=started + timedelta(minutes=rng.randint(2, 9)))
                for host_name in ("web-01", "web-02"):
                    for order, label in enumerate(("check", "patch", "verify")):
                        state = "failed" if (failed and order == 1 and host_name == "web-02") else "completed"
                        t = Task.objects.create(
                            host=hosts[host_name], requested_by=admin, run=run, step_order=order,
                            step_label=label, action="_script", state=state, nonce=secrets.token_hex(32),
                            result_output="ok" if state == "completed" else "apt: lock held by another process")
                        Task.objects.filter(pk=t.pk).update(
                            created_at=started, completed_at=started + timedelta(minutes=order + 1))
        resp = web.post("/api/v1/reports/", data=json.dumps({
            "name": "Web tier patching", "range_days": 30, "playbooks": [str(playbook.id)],
            "widgets": [
                {"kind": "success_rate", "x": 0, "y": 0, "w": 4, "h": 3},
                {"kind": "runs_per_day", "x": 4, "y": 0, "w": 8, "h": 3},
                {"kind": "host_outcomes", "x": 0, "y": 3, "w": 6, "h": 3},
                {"kind": "duration", "x": 6, "y": 3, "w": 6, "h": 3},
            ]}), content_type="application/json")
        if resp.status_code not in (200, 201):
            say(f"report: {resp.status_code} {resp.content[:300]}")

    # Update policies.
    nightly = UpdatePolicy.objects.create(
        name="Prod nightly patching", target_tags=["prod"], cron_hour="2", window_hours=3,
        patch_enabled=True, linux_updates="security", reboot="if_needed", created_by=admin)
    for app, state in (("openssl", "latest"), ("nginx", "latest"), ("postgresql-16", "pinned")):
        AppRule.objects.create(policy=nightly, app=app, state=state)
    UpdatePolicy.objects.create(
        name="Windows second Tuesday", target_tags=["windows"], cron_hour="22", cron_dow="2",
        window_hours=4, patch_enabled=True, windows_classifications=["security", "critical"],
        deferral_days=7, reboot="if_needed", created_by=admin)

    # Vulnerabilities, through the Anvil importer the product uses.
    try:
        from apps.vulns.scanners.anvil import ingest_record
        from apps.vulns.test_anvil import _result, record

        findings = [
            _result(1, host="web-01", purl="pkg:deb/ubuntu/openssl@3.0.13-0ubuntu3.1", cves=("CVE-2024-6119",)),
            _result(2, host="web-02", purl="pkg:deb/ubuntu/openssl@3.0.13-0ubuntu3.1", cves=("CVE-2024-6119",)),
            _result(3, host="docker-01", purl="pkg:deb/ubuntu/curl@7.81.0-1ubuntu1.16", cves=("CVE-2024-2398",), kev=True),
            _result(4, host="db-01", purl="pkg:deb/debian/libxml2@2.9.14+dfsg-1.3", cves=("CVE-2024-25062",), level="warning"),
            _result(5, host="db-02", purl="pkg:deb/debian/libxml2@2.9.14+dfsg-1.3", cves=("CVE-2024-25062",), level="warning"),
            _result(6, host="edge-gw-01", purl="pkg:deb/debian/openssh-server@1:9.2p1-2", cves=("CVE-2024-6387",), kev=True),
            _result(7, host="ci-runner-01", purl="pkg:deb/ubuntu/git@2.43.0-1ubuntu7", cves=("CVE-2024-32002",)),
        ]
        say(f"vulns: {ingest_record(record(*findings))}")
        from apps.vulns.models import VulnFinding

        fixes = {"openssl": "3.0.13-0ubuntu3.4", "curl": "7.81.0-1ubuntu1.18",
                 "libxml2": "2.9.14+dfsg-1.3+deb12u1", "openssh-server": "1:9.2p1-2+deb12u3"}
        for finding in VulnFinding.objects.all():
            if finding.package_name in fixes:
                finding.fixed_version = fixes[finding.package_name]
                finding.save()
    except Exception as exc:  # noqa: BLE001 — a demo without findings still demos
        say(f"vulns skipped: {exc}")

    # A public status page with ninety days of uptime behind it.
    page = StatusPage.objects.create(
        title="Acme Services", enabled=True,
        host_ids=[str(hosts[n].id) for n in ("web-01", "web-02", "db-01", "edge-gw-01", "files-01")],
        host_labels={str(hosts["web-01"].id): "Website", str(hosts["web-02"].id): "Customer portal",
                     str(hosts["db-01"].id): "Database", str(hosts["edge-gw-01"].id): "VPN gateway",
                     str(hosts["files-01"].id): "File shares"})
    samples = []
    for name in ("web-01", "web-02", "db-01", "edge-gw-01", "files-01"):
        for hours in range(90 * 24, 0, -1):
            down = (name == "edge-gw-01" and hours in (300, 301)) or (name == "files-01" and hours == 1500)
            samples.append(HostUptimeSample(host=hosts[name], time=NOW - timedelta(hours=hours), up=not down))
    HostUptimeSample.objects.bulk_create(samples, ignore_conflicts=True)
    say(f"status page /status/{page.token}/")

    # Free allows one relay in use: the office relay, with the built-in one off.
    Relay.objects.filter(kind="builtin").update(enabled=False)
    for name, location, url, connected in (
        ("Harrisburg office", "Harrisburg, PA", "https://relay-hbg.example.invalid", True),
    ):
        Relay.objects.create(name=name, location=location, url=url, connected=connected,
                             last_seen_at=NOW - timedelta(seconds=rng.randint(5, 40)) if connected else NOW - timedelta(days=2),
                             last_error="" if connected else "connection refused")

say("done: sign in as demo / demo-pass-123 (TOTP JBSWY3DPEHPK3PXP)")
