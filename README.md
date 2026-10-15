<div align="center">

<img src="docs/screenshots/banner.png" alt="Vigil: self-hosted infrastructure monitoring and endpoint management" width="720">

[Quick start](#quick-start) ·
[Features](#features) ·
[Editions](#editions) ·
[Documentation](#documentation)

A [Susquehanna Syntax](https://github.com/Susquehanna-Syntax) product.

</div>

<br>

![The Vigil dashboard: stat tiles and a grid of host cards showing live CPU, memory, disk and network](docs/screenshots/dashboard.png)

Vigil is a lightweight monitoring system. A small agent on each of your hosts
checks in with a central Django server. The server collects metrics, fires
alerts, keeps a hardware inventory, and sends signed tasks back to the agents
to change things. It runs on Linux, Windows and macOS hosts, on a single
`docker compose up`, with unlimited hosts and retention for free.

## Features

### See everything

Live CPU, memory, disk, network and load for every host, with history from one
hour to seven days. Build your own dashboards from a catalogue of widgets,
keep several, and share them.

![The Monitor page for one host: gauges for CPU, memory and disk, and a CPU history chart](docs/screenshots/monitor.png)

### Know what you own

A hardware and software inventory of the whole fleet: OS, CPU, RAM, BIOS,
disks, MAC addresses, uptime, last logged-in user. Sort, filter, reorder the
columns, export to CSV, and add your own columns from a task's output.
Tag hosts however you like and act on a whole tag at once.

![The Inventory page: a sortable hardware table with one row per host](docs/screenshots/inventory.png)

### Get told when something is wrong

Twenty alert rules ship ready to use, with auto-resolve and host-offline
detection. Alerts go out by webhook or email, can open tickets, and can be
acknowledged or snoozed. **Suggest Fix** drafts a remediation task for you to
review before it runs, using an AI model of your choice (bring your own key,
any OpenAI-compatible endpoint or Anthropic) when one is needed.

![The Alerts page: firing alerts with Suggest Fix and Ack buttons](docs/screenshots/alerts.png)

### Fix what is vulnerable

Findings from Trivy, Nessus and Greenbone, plus Vigil's own matcher against
OSV, CISA KEV and EPSS that works offline. Findings are grouped by the fix that
resolves them, so one **Deploy fix** patches every affected host at once.

![The Vulnerabilities page: fleet score, severity totals, and one row per fix with a Deploy fix button](docs/screenshots/vulns.png)

### Run Docker and Podman

Containers and compose stacks across your hosts: start, stop, restart, read
logs, update images, roll back, and edit a stack and its `.env` in place.

![The Containers page: compose stacks and their containers with state, CPU and memory](docs/screenshots/containers.png)

### And more

| | |
|---|---|
| **Tasks and playbooks** | Write multi-step tasks in YAML with schedule windows, retries and success criteria. Deploy by host or tag, or let a baseline run on every newly approved host. Browse and submit to the [community catalog](https://github.com/Susquehanna-Syntax/Vigil-Approved-Scripts). |
| **Patching and app policies** | Set an app to present, latest, pinned or absent. Patch inside maintenance windows, defer, approve each change, and track the percent of hosts patched within your SLA. |
| **Firewall and topology** | Read firewall rules on demand, see your hosts grouped by site with their ports and connections, and get a warning before a change would lock you out over SSH or RDP. |
| **Reprovisioning** | Rebuild a host remotely from an image library and install profiles. |
| **Status pages** | Publish a public status page from the same data. |
| **Active Directory** | Import computers and auto-tag them from their OU. |

### Built to be safe to run

- Every task is **signed with Ed25519**. Agents pin the server key on first use
  and refuse tasks if it ever changes.
- Each agent runs in one of three modes, **monitor**, **managed** or **full
  control**, and the agent's own config is the final say. A compromised server
  cannot raise an agent's privileges. Monitor mode runs unprivileged.
- Risky steps and host approval can require a **TOTP code**, single-use with
  replay protection.
- No `shell=True` in the agent, and every task parameter is validated before it
  runs.

## Quick start

You need Docker with the Compose plugin.

```bash
git clone https://github.com/Susquehanna-Syntax/Vigil.git
cd Vigil
make setup        # copies .env.example to .env
make gen-key      # prints a signing key seed
make gen-secret   # prints a Django secret key
```

Open `.env` and paste in `VIGIL_SIGNING_KEY_SEED`, `DJANGO_SECRET_KEY`, and a
`POSTGRES_PASSWORD`. Then:

```bash
make up           # starts Django, TimescaleDB, Redis, Celery
make superuser    # create your admin login
```

Open **http://localhost:9200** and sign in.

> Pasting the stack into Portainer, or any UI that round-trips through YAML?
> Read the note on `VIGIL_SIGNING_KEY_SEED` in the
> [reference](docs/REFERENCE.md#quick-start-docker-compose) first. A mangled
> seed breaks every agent check-in.

### Add your first host

1. In the dashboard click **Add Host**, pick an operating system and a mode.
2. Copy the one-line install command it shows and run it on the machine:

   ```bash
   # Linux / macOS
   curl -fsSL https://vigil.example.com/agent/install.sh | sudo env VIGIL_TOKEN=<token> bash
   ```

   ```powershell
   # Windows, in an elevated PowerShell
   $env:VIGIL_TOKEN = "<token>"; irm https://vigil.example.com/agent/install.ps1 | iex
   ```

3. The host appears in **Settings → Enrollment Queue**. Approve it and metrics
   start flowing within a few seconds.

Start in **monitor** mode. You can move a host to managed or full control later
with one config line and a re-run of the installer.

### Your first task

Tasks are plain YAML. This one reloads nginx and checks that it came back:

```yaml
name: Reload nginx
description: Reload nginx and confirm it is active.
risk: low
actions:
  - id: reload
    type: reload_service
    params:
      service_name: nginx
  - id: verify
    type: check_service
    params:
      service_name: nginx
      expect: active
```

Paste it into **Tasks**, pick a host or a tag, and deploy.

### Running without Docker

For a quick look on a laptop, Vigil runs on SQLite with no other services:

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export USE_SQLITE=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

This is for development. Use the Compose stack for anything real.

## Editions

Free Vigil answers "is my stuff up"; Business Vigil answers "can I prove to a
third party that their stuff is up." Monitoring, alerting, and unlimited
agents, hosts, and retention are free forever. The Business tier adds
accountability features: sites, an audit log, roles, and branding. Nothing ever
blocks: an expired license switches Business features off and monitoring
carries on untouched. See [docs/EDITIONS.md](docs/EDITIONS.md) for the full
feature matrix.

## Documentation

| | |
|---|---|
| [Reference manual](docs/REFERENCE.md) | Install options, environment variables, upgrading, backups, every task action, the API |
| [Agent privileges](docs/AGENT-PRIVILEGES.md) | What each agent mode can and cannot do |
| [Remote access](docs/REMOTE-ACCESS.md) | Checking in from off the LAN, reverse proxy, Cloudflare Tunnel |
| [Reprovisioning](docs/reprovisioning.md) | Rebuilding hosts from an image library |
| [Roles and permissions](docs/rbac.md) | Who can do what |
| [Security policy](.github/SECURITY.md) | Reporting a vulnerability |
| [Contributing](.github/CONTRIBUTING.md) | Inbound license, sign-off, and how to send a change |

## License

AGPLv3 for everything except `server/apps_business/`, which is source-visible
under a commercial license (`server/apps_business/LICENSE`) and requires a
Vigil Business subscription for production use. See [LICENSE](LICENSE).
