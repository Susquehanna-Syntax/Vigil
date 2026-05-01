# Vigil

**Smart, self-hosted infrastructure monitoring and endpoint management.**

A [Susquehanna Syntax](https://sqsy.dev) product.

---

## Overview

Vigil is a lightweight monitoring system where agents on your hosts phone home to a central Django server. The server collects metrics, evaluates alert rules, dispatches signed tasks back to agents, and maintains a hardware inventory of your fleet. Everything runs over HTTPS with Ed25519 task signing and TOFU key pinning.

**Key features:**
- Real-time metric collection (CPU, memory, disk, network, swap, load average, processes)
- 20 built-in alert rules with auto-resolution and host-offline detection
- Notification dispatch (webhook, email)
- Hardware inventory with OS, CPU, RAM, BIOS, MAC, uptime, timezone, and custom collector columns
- Nessus/Tenable vulnerability scan integration
- Active Directory computer import with auto-tagging from OU paths
- Tag-based fleet segmentation — deploy tasks by tag or by individual host
- Multistep task authoring (YAML editor) with schedule windows, retry policies, and success criteria
- Community task sharing (publish/fork templates)
- TOTP-based two-factor authentication for task execution
- Signed remote task execution with mode/allowlist enforcement on the agent
- SQSY dark-theme dashboard with Chart.js visualizations

---

## Quick Start (Local Dev — SQLite)

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
USE_SQLITE=true .venv/bin/python manage.py migrate
USE_SQLITE=true .venv/bin/python manage.py createsuperuser
USE_SQLITE=true .venv/bin/python manage.py runserver
```

- Dashboard: http://localhost:8000
- Health check: http://localhost:8000/api/v1/health/
- Admin: http://localhost:8000/admin/

`USE_SQLITE=true` switches the database engine to SQLite and bypasses the need for PostgreSQL and TimescaleDB.

---

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env — set DJANGO_SECRET_KEY and VIGIL_SIGNING_KEY_SEED at minimum
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

This brings up the full stack: Django, PostgreSQL + TimescaleDB, Redis, Celery worker, and Celery beat.

Generate a signing key seed (required for task deployment):

```bash
python3 -c "import base64; from nacl.signing import SigningKey; print(base64.b64encode(bytes(SigningKey.generate())).decode())"
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | `insecure-dev-key-…` | Django secret key — **change in production** |
| `DJANGO_DEBUG` | `true` | Set to `false` in production |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `USE_SQLITE` | _(unset)_ | Set to `true` to use SQLite instead of PostgreSQL |
| `POSTGRES_DB` | `vigil` | PostgreSQL database name |
| `POSTGRES_USER` | `vigil` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `vigil` | PostgreSQL password |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | Redis URL for Celery |
| `VIGIL_SIGNING_KEY_SEED` | _(empty)_ | Base64 Ed25519 seed — required for task deployment |
| `VIGIL_TIMEZONE` | `UTC` | IANA timezone for schedule window evaluation (e.g. `America/New_York`) |
| `VIGIL_METRIC_RETENTION_DAYS` | `30` | Days to keep metric history |
| `NESSUS_URL` | _(empty)_ | Nessus/Tenable server URL |
| `NESSUS_ACCESS_KEY` | _(empty)_ | Nessus API access key |
| `NESSUS_SECRET_KEY` | _(empty)_ | Nessus API secret key |
| `NESSUS_VERIFY_SSL` | `true` | Verify Nessus TLS certificate |
| `EMAIL_BACKEND` | `console` | Django email backend |
| `EMAIL_HOST` | `localhost` | SMTP host |
| `EMAIL_PORT` | `587` | SMTP port |
| `VIGIL_NOTIFICATION_FROM_EMAIL` | `vigil@localhost` | From address for alert emails |

---

## Running the Agent

### Local dev (agent + server on the same machine)

```bash
cd agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yml agent.yml
```

Edit `agent.yml`:

```yaml
server_url: http://localhost:8000
mode: monitor            # start with monitor, upgrade later
checkin_interval: 15     # faster for testing
data_dir: ./data
```

Run the agent:

```bash
python3 -m vigil_agent -c agent.yml --log-level DEBUG
```

On first run the agent will:
- Generate a cryptographic token and save it to `agent.yml`
- Register with the server (creates a **pending** host)
- Start checking in — the server responds with `{"status": "pending"}` until you approve

Approve the host from **Settings → Enrollment Queue** in the dashboard, or via API:

```bash
curl -X POST http://localhost:8000/api/v1/hosts/<host-id>/approve/ \
  -H "Cookie: sessionid=<your-session>" \
  -H "X-CSRFToken: <csrf-token>"
```

### Production (remote hosts, TLS)

```yaml
# /etc/vigil/agent.yml on each monitored host
server_url: https://vigil.yourdomain.com
mode: managed
checkin_interval: 60
data_dir: /var/lib/vigil-agent
tags:
  - web-server
  - prod
allowlist:
  - restart_service
  - restart_container
  - clear_temp_files
  - run_package_updates
```

**Security notes:**
- `chmod 600 agent.yml` — the config contains the agent token
- **TOFU key pinning:** the first public key received from the server is pinned to `data_dir/server_public_key.pin`. Any future key change is treated as a potential compromise — all tasks are rejected until the pin file is manually deleted
- **Agent mode is authoritative** — a compromised server cannot escalate an agent's mode or allowlist
- No `shell=True` anywhere; all task parameters are validated before execution

---

## Dashboard Pages

| Page | Description |
|---|---|
| **Dashboard** | Host card grid — status dots, CPU/Memory/Disk/Network mini-bars, RDP, Deploy, Remove buttons. Searchable. Inactive agents (90+ days) collapse into a separate section. |
| **Inventory** | Hardware table for all enrolled hosts. Columns: hostname, IP, OS, CPU, RAM, MAC, BIOS, disks, uptime, last user, timezone, and more. Scrollable, sortable (click header), filterable per-column (`=value` for exact, default contains), drag-to-reorder columns, column visibility toggled via Columns button. |
| **Tasks** | YAML task editor, your private library, deployment history, and community templates. |
| **Vulns** | Nessus/Tenable scan findings per host. |
| **Monitor** | Select a host for live SVG gauges (CPU, Memory, Disk, Load) and Chart.js time-series with 1h/6h/24h/7d range selector. RDP download button for Windows hosts. |
| **Alerts** | Firing, acknowledged, and resolved alerts across the fleet. |
| **Community** | Browse and fork task templates published by other users on this server. |
| **Settings** | Enrollment queue (approve/reject pending hosts), TOTP enrollment, AD import, server timezone display. |

---

## Hardware Inventory

Agents ship a hardware snapshot on an hourly cadence (separate from the 60-second metric checkin). The inventory page shows:

| Field | Source |
|---|---|
| OS (agent) | Agent-reported `host.os` string |
| OS Name | `/etc/os-release PRETTY_NAME` |
| OS Version | `/etc/os-release VERSION_ID` |
| Kernel | `platform.release()` |
| Architecture | `platform.machine()` |
| Uptime | `time.time() - psutil.boot_time()` |
| Last User | `psutil.users()` — most recent login |
| Manufacturer | `/sys/class/dmi/id/sys_vendor` |
| Model | `/sys/class/dmi/id/product_name` |
| Service Tag | `/sys/class/dmi/id/product_serial` |
| BIOS | `/sys/class/dmi/id/bios_version` + `bios_date` |
| RAM | `psutil.virtual_memory().total` |
| CPU | `/proc/cpuinfo model name` |
| Cores | `psutil.cpu_count(logical=True)` |
| MAC | `psutil.net_if_addrs()` — preferred eth/en interface |
| Disks | `psutil.disk_partitions()` |
| Timezone | `/etc/timezone` |

**Custom columns** — tasks marked with a `collect:` block in their YAML write key/value pairs into `HostInventory.custom_columns`, which auto-appear as additional columns on the Inventory page.

---

## Tags

Tags are free-form strings attached to hosts. They enable tag-based task deployment and fleet segmentation.

**Sources (merged in order):**
1. `agent.yml` — `tags: [web-server, prod, rack-3]` — sent at each checkin
2. Server-side tags — editable in the host detail panel (click any host card)
3. Auto-tags — applied at checkin based on OS (`linux`, `windows`, `macos`) and mode (`managed`, `monitor`, `full_control`)
4. AD import — tags from OU path segments (e.g. `OU=Servers,OU=IT` → `servers`, `it`)

**Deploy by tag** — in the deploy modal, switch the target toggle from "Individual Hosts" to "By Tag" to deploy to all online managed hosts with a given tag.

---

## Task Authoring

Tasks are YAML definitions authored in the built-in editor (Tasks → New Task) and deployed across hosts via the deploy modal.

### Full YAML schema

```yaml
name: Restart nginx and verify
description: Gracefully reload nginx, confirm the service is running.
relevance: web servers
risk: standard   # low | standard | high

# Optional inputs — filled in at deploy time
inputs:
  - id: service
    label: Service name
    type: text
    default: nginx
    required: true

# Optional: restrict dispatch to a maintenance window (server timezone)
schedule:
  window:
    start_hour: 8       # 0–23
    start_minute: 0     # 0–59, default 0
    end_hour: 17        # inclusive through end_hour:59
    end_minute: 0
    days: [mon, tue, wed, thu, fri]   # default: all 7

# Optional: retry failed steps
on_failure:
  retry:
    attempts: 3         # 0 = no retry
    delay_seconds: 60

# Optional: validate step output (supports {{ inputs.x }} variables)
success_criteria:
  exit_code: 0
  output_contains: "active (running)"   # substring match
  output_regex: "^OK"                   # regex (applied after output_contains)

actions:
  - id: reload
    type: reload_service
    params:
      service_name: "{{ inputs.service }}"
    success_criteria:
      exit_code: 0
      output_contains: "{{ inputs.service }} reloaded"

  - id: verify
    type: check_service
    params:
      service_name: "{{ inputs.service }}"
      expect: active
```

**Schedule windows** are evaluated in the server's `VIGIL_TIMEZONE`. Tasks outside the window stay `PENDING` and are dispatched on the next checkin that falls inside the window.

**Retry** — on step failure the agent re-runs the step after `delay_seconds`, up to `attempts` times, before marking the task as failed.

**Success criteria** — even a zero exit code is treated as failure if `output_contains` or `output_regex` doesn't match. Per-step criteria override the top-level criteria.

### Available actions

All 37 primitives are defined in `server/apps/tasks/spec.py` and executed in `agent/vigil_agent/executor.py`. `run_command` and `execute_script` require `full_control` mode; all others require `managed` or higher.

**Service management**

| Action | Params | Optional |
|---|---|---|
| `restart_service` | `service_name` | — |
| `start_service` | `service_name` | — |
| `stop_service` | `service_name` | — |
| `reload_service` | `service_name` | — |
| `enable_service` | `service_name` | — |
| `disable_service` | `service_name` | — |
| `check_service` | `service_name` | `expect` |

**Container management**

| Action | Params | Optional |
|---|---|---|
| `restart_container` | `container_name` | — |
| `start_container` | `container_name` | — |
| `stop_container` | `container_name` | — |
| `remove_container` | `container_name` | — |
| `pull_image` | `image` | — |
| `docker_compose_up` | `compose_file` | `services` |
| `docker_compose_down` | `compose_file` | — |
| `clear_docker_logs` | — | `container_name` |

**Package management**

| Action | Params | Optional |
|---|---|---|
| `install_package` | `package_name` | — |
| `remove_package` | `package_name` | — |
| `update_package` | `package_name` | — |
| `run_package_updates` | — | `security_only` |

**File operations**

| Action | Params | Optional |
|---|---|---|
| `write_file` | `path`, `content` | `mode` |
| `create_directory` | `path` | `owner`, `group`, `mode` |
| `delete_path` | `path` | `recursive` |
| `copy_file` | `src`, `dest` | — |
| `move_file` | `src`, `dest` | — |
| `set_permissions` | `path` | `owner`, `group`, `mode` |

**System**

| Action | Params | Optional |
|---|---|---|
| `clear_temp_files` | — | `older_than_days` |
| `execute_script` | `script_name` | — |
| `reboot` | — | `delay_seconds` |
| `run_command` | `command` | `timeout` |
| `set_hostname` | `hostname` | — |

**Networking**

| Action | Params | Optional |
|---|---|---|
| `add_firewall_rule` | `port`, `protocol` | `action` |
| `remove_firewall_rule` | `port`, `protocol` | — |

**User management**

| Action | Params | Optional |
|---|---|---|
| `create_user` | `username` | `groups`, `shell` |
| `delete_user` | `username` | `remove_home` |
| `add_user_to_group` | `username`, `group` | — |

**Cron**

| Action | Params | Optional |
|---|---|---|
| `create_cron_job` | `schedule`, `command` | `user` |
| `delete_cron_job` | `pattern` | `user` |

### Deployment flow

1. Write a task definition in the YAML editor (Tasks → New Task)
2. Click **Deploy** on a library card
3. Fill in any inputs on the **Inputs** tab
4. Optionally set a schedule window, retry policy, and success criteria on their tabs
5. Select target hosts (or choose a tag) on the **Hosts** tab
6. Enter your 6-digit TOTP code and submit
7. Track execution in the run detail view (Tasks → History)

---

## Alerting

Vigil ships with **20 default alert rules** created automatically on first migration. Rules evaluate every 60 seconds via Celery beat. Alerts auto-resolve when the metric returns below the threshold.

**CPU**

| Rule | Threshold | Severity | Duration |
|---|---|---|---|
| Elevated CPU Usage | > 75% | Warning | 5 min |
| High CPU Usage | > 90% | Critical | 5 min |
| CPU Critical (95%) | > 95% | Critical | 1 min |
| High Load Average (1m) | > 10 | Warning | 2 min |
| High Load Average (5m) | > 8 | Warning | 5 min |
| Sustained High Load (15m) | > 6 | Critical | 10 min |

**Memory & Swap**

| Rule | Threshold | Severity | Duration |
|---|---|---|---|
| Elevated Memory Usage | > 80% | Warning | 2 min |
| High Memory Usage | > 90% | Critical | 2 min |
| Memory Critical (95%) | > 95% | Critical | 1 min |
| High Swap Usage | > 50% | Warning | 5 min |
| Swap Nearly Exhausted | > 80% | Critical | 2 min |

**Disk**

| Rule | Threshold | Severity | Duration |
|---|---|---|---|
| Disk Usage High | > 80% | Warning | Instant |
| Disk Nearly Full | > 90% | Critical | Instant |
| Disk Critical (95%) | > 95% | Critical | Instant |

**Network**

| Rule | Threshold | Severity | Duration |
|---|---|---|---|
| High Network Error Rate (In) | > 100 errors | Warning | 2 min |
| High Network Error Rate (Out) | > 100 errors | Warning | 2 min |
| High Network Drop Rate (In) | > 200 drops | Warning | 2 min |
| High Network Drop Rate (Out) | > 200 drops | Warning | 2 min |

**Process**

| Rule | Threshold | Severity | Duration |
|---|---|---|---|
| Process CPU Spike | > 95% (single process) | Warning | 2 min |
| Process Memory Spike | > 50% (single process) | Warning | 2 min |

**Host offline** — when a host misses 5+ minutes of checkins, an alert is automatically created. It auto-resolves on the next successful checkin.

Custom rules can be created via the Django admin.

---

## Notifications

Configure notification channels in the Django admin under **Alerts → Notification channels**:

- **Webhook** — POST JSON payload to a URL. Set a `secret` in the config for an `X-Vigil-Secret` request header.
- **Email** — Sent via Django's email backend. Configure `EMAIL_HOST`, `EMAIL_PORT`, etc. in your `.env`.

---

## Two-Factor Authentication (TOTP)

Vigil implements RFC 6238 TOTP natively. Task deployments require a 6-digit TOTP code once enrolled.

**Enrollment:**
1. Go to Settings → Two-Factor Authentication
2. Click **Enroll TOTP** — copy the secret into any authenticator app (Google Authenticator, Authy, 1Password, Bitwarden, Aegis)
3. Enter a code from the app to confirm

Task deploys are blocked until enrolled. TOTP can be disabled from Settings (requires a current code).

---

## Vulnerability Management (Nessus)

Set `NESSUS_URL`, `NESSUS_ACCESS_KEY`, and `NESSUS_SECRET_KEY` in your `.env`. Vigil syncs scan findings from Nessus/Tenable once per hour via Celery beat and correlates them to hosts by IP address. Findings appear on the **Vulns** page grouped by severity.

---

## Active Directory Import

Configure AD in **Settings → Active Directory**:

- LDAP server URL, bind DN, bind password, base DN, and the OU containing computer objects
- **Import Now** runs a Celery task that queries LDAP for computer objects, creates `PENDING` host records for any not already enrolled, and auto-tags them from their OU path (e.g. `OU=Servers,OU=IT` → tags `servers`, `it`)

---

## Agent Modes

| Mode | Metrics | Tasks |
|---|---|---|
| `monitor` | Collected | Ignored entirely |
| `managed` | Collected | Only allowlisted actions |
| `full_control` | Collected | Any action |

The allowlist is defined in `agent.yml` and enforced locally by the agent — the server cannot override it.

---

## API Reference

### Agent-facing (Bearer token)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/register` | Agent self-registration (creates pending host) |
| `POST` | `/api/v1/checkin` | Metric ingest + hardware inventory + task dispatch |
| `POST` | `/api/v1/tasks/result/` | Report task execution outcome |

### Hosts

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/hosts/` | List enrolled hosts |
| `GET` `DELETE` | `/api/v1/hosts/{id}/` | Host detail / remove host and all data |
| `POST` | `/api/v1/hosts/{id}/approve/` | Approve pending enrollment |
| `POST` | `/api/v1/hosts/{id}/reject/` | Reject pending enrollment |
| `POST` | `/api/v1/hosts/{id}/poll/` | Request immediate checkin |
| `GET` | `/api/v1/hosts/{id}/rdp/` | Download `.rdp` file (Windows hosts) |
| `GET` `PATCH` | `/api/v1/hosts/{id}/tags/` | Get / update host tags |
| `GET` | `/api/v1/hosts/tags/` | All tags in use across the fleet with host counts |
| `GET` | `/api/v1/hosts/inventory/` | Inventory list for all hosts |
| `GET` | `/api/v1/hosts/{id}/inventory/` | Inventory detail for one host |
| `GET` `PUT` | `/api/v1/hosts/ad/` | AD configuration |
| `POST` | `/api/v1/hosts/ad/sync/` | Trigger AD import now |

### Metrics

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/metrics/{host}/{cat}/{metric}/` | Metric history (supports `?from=`, `?to=`, `?limit=`) |

### Alerts

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/alerts/` | List alerts (`?state=firing\|acknowledged\|resolved`) |
| `POST` | `/api/v1/alerts/{id}/acknowledge/` | Acknowledge a firing alert |
| `POST` | `/api/v1/alerts/{id}/silence/` | Silence a firing alert |

### Tasks

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/tasks/actions/` | Full action registry |
| `GET` `POST` | `/api/v1/tasks/definitions/` | List / create task definitions |
| `POST` | `/api/v1/tasks/definitions/validate/` | Validate YAML without saving |
| `GET` `PUT` `DELETE` | `/api/v1/tasks/definitions/{id}/` | Read / update / delete a definition |
| `POST` | `/api/v1/tasks/definitions/{id}/fork/` | Fork a community template |
| `POST` | `/api/v1/tasks/definitions/{id}/publish/` | Publish to community |
| `POST` | `/api/v1/tasks/definitions/{id}/unpublish/` | Unpublish from community |
| `POST` | `/api/v1/tasks/definitions/{id}/deploy/` | Deploy across hosts (requires TOTP) |
| `GET` | `/api/v1/tasks/runs/{id}/` | Run detail with per-host step status |

### Misc

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/vulns/` | Vulnerability findings |
| `GET` | `/api/v1/accounts/totp/` | TOTP enrollment status |
| `POST` | `/api/v1/accounts/totp/enroll/` | Start TOTP enrollment |
| `POST` | `/api/v1/accounts/totp/enroll/confirm/` | Confirm with 6-digit code |
| `POST` | `/api/v1/accounts/totp/disable/` | Disable TOTP |
| `GET` | `/api/v1/health/` | Health check (no auth) |

---

## Project Layout

```
Vigil/
├── docker-compose.yml
├── .env.example
├── agent/                       # Python monitoring agent
│   ├── config.example.yml       # Annotated agent config template
│   ├── requirements.txt
│   └── vigil_agent/
│       ├── __main__.py          # Main loop: register → checkin → collect → execute
│       ├── client.py            # HTTPS client (register, checkin, report result)
│       ├── collector.py         # psutil metrics + hardware inventory collection
│       ├── config.py            # YAML config loading + token generation
│       ├── executor.py          # Task execution, mode/allowlist enforcement
│       ├── runtime.py           # Multi-step task runtime with success criteria
│       ├── verify.py            # Ed25519 signature verification + TOFU key pinning
│       └── nonce_store.py       # Replay protection (SQLite-backed nonce store)
└── server/
    ├── Dockerfile
    ├── requirements.txt
    ├── manage.py
    ├── vigil/
    │   ├── settings.py          # All settings (SQLite fallback via USE_SQLITE=true)
    │   ├── celery.py
    │   ├── signing.py           # Ed25519 task signing (key loaded from env)
    │   └── urls.py              # URL config + dashboard view
    ├── templates/
    │   ├── dashboard.html       # Full SQSY single-page dashboard
    │   └── _host_card.html      # Host card partial (included in dashboard)
    └── apps/
        ├── hosts/               # Host model, enrollment, checkin, inventory, tags, AD import
        ├── metrics/             # MetricPoint model + metric history API
        ├── alerts/              # AlertRule, Alert, NotificationChannel, Celery evaluation
        ├── tasks/               # TaskDefinition (YAML), Task, TaskRun — authoring + deploy
        ├── vulns/               # Nessus vulnerability sync + findings API
        └── accounts/            # UserProfile, TOTP enrollment (RFC 6238 from scratch)
```

---

## License

AGPLv3
