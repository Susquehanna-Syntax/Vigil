# Vigil

**Smart, self-hosted infrastructure monitoring and endpoint management.**

A [Susquehanna Syntax](https://sqsy.dev) product.

---

## Overview

Vigil is a lightweight monitoring system where agents on your hosts phone home to a central Django server. The server collects metrics, evaluates alert rules, and dispatches signed tasks back to agents. Everything runs over HTTPS with Ed25519 task signing and TOFU key pinning.

**Key features:**
- Real-time metric collection (CPU, memory, disk, network, swap)
- Threshold-based alerting with auto-resolution
- Notification dispatch (webhook, email)
- Multistep task authoring (YAML editor), community sharing, and fleet deployment
- TOTP-based two-factor authentication for task execution
- Signed remote task execution with mode/allowlist enforcement
- SQSY dark-theme dashboard with Chart.js visualizations

## Quick Start (Local Dev)

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate --settings=vigil.settings_local
.venv/bin/python manage.py createsuperuser --settings=vigil.settings_local
.venv/bin/python manage.py runserver --settings=vigil.settings_local
```

Dashboard: [http://localhost:8000](http://localhost:8000) (log in with your superuser credentials)
Health check: [http://localhost:8000/api/v1/health/](http://localhost:8000/api/v1/health/)
Admin: [http://localhost:8000/admin/](http://localhost:8000/admin/)

Local dev uses SQLite and runs Celery tasks synchronously in-process — no Redis or Postgres needed.

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env — set DJANGO_SECRET_KEY and VIGIL_SIGNING_KEY_SEED
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

This brings up the full stack: Django, PostgreSQL + TimescaleDB, Redis, Celery worker, and Celery beat.

Generate a signing key seed:

```bash
python3 -c "import base64; from nacl.signing import SigningKey; print(base64.b64encode(bytes(SigningKey.generate())).decode())"
```

## Testing with the Agent

### Local dev (agent + server on the same machine)

**1. Start the server** (see Quick Start above).

**2. Set up the agent:**

```bash
cd agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yml agent.yml
```

**3. Edit `agent.yml`:**

```yaml
server_url: http://localhost:8000
agent_token: ""          # auto-generated on first run
mode: monitor            # start with monitor, upgrade later
checkin_interval: 15     # faster interval for testing
data_dir: ./data         # local dir instead of /var/lib
```

**4. Run the agent:**

```bash
.venv/bin/python -m vigil_agent -c agent.yml --log-level DEBUG
```

On first run the agent will:
- Generate a cryptographic token and save it to `agent.yml`
- Register with the server (creates a **pending** host)
- Start checking in — the server will respond with `{"status": "pending"}` until you approve

**5. Approve the host:**

Log into the dashboard at [http://localhost:8000](http://localhost:8000), go to **Settings** (gear icon), and click **Approve** on the pending host. Alternatively, use the admin panel or the API:

```bash
# Find the host ID
curl -s http://localhost:8000/api/v1/hosts/ \
  -H "Cookie: sessionid=<your-session-cookie>" | python3 -m json.tool

# Approve it
curl -X POST http://localhost:8000/api/v1/hosts/<host-id>/approve/ \
  -H "Cookie: sessionid=<your-session-cookie>" \
  -H "X-CSRFToken: <csrf-token>"
```

**6. Verify metrics are flowing:**

Once approved, the next agent checkin will start ingesting metrics. You should see data on the dashboard within one checkin cycle. Check the **Monitor** page — select the host from the dropdown to see live gauges and charts.

### Docker Compose (agent on host, server in Docker)

**1. Start the server stack:**

```bash
cp .env.example .env
# Set DJANGO_SECRET_KEY, VIGIL_SIGNING_KEY_SEED in .env
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

**2. Set up the agent** (on the host machine or another machine):

```bash
cd agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yml agent.yml
```

**3. Edit `agent.yml`:**

```yaml
# If agent is on the same machine as Docker:
server_url: http://localhost:8000

# If agent is on a different machine:
# server_url: https://your-vigil-server:8000

agent_token: ""
mode: managed
checkin_interval: 60
data_dir: ./data
allowlist:
  - restart_service
  - clear_temp_files
```

**4. Run and approve** (same as local dev above).

### Production (agent on remote hosts, server behind TLS)

In production, the server should be behind a TLS-terminating reverse proxy (nginx, Caddy, etc.). The agent enforces TLS certificate verification by default and provides no option to disable it — this is intentional.

```yaml
# agent.yml on each monitored host
server_url: https://vigil.yourdomain.com
agent_token: ""
mode: managed
checkin_interval: 60
data_dir: /var/lib/vigil-agent
scripts_dir: /etc/vigil/scripts
allowlist:
  - restart_service
  - restart_container
  - clear_temp_files
```

**Security notes:**
- Set `chmod 600 agent.yml` — the config contains the agent token
- The agent uses TOFU (Trust On First Use) key pinning: the first public key it receives from the server is pinned to `data_dir/server_public_key.pin`. Any future key change is treated as a potential compromise and all tasks are rejected until the pin file is manually deleted.
- Agent mode is enforced locally — a compromised server cannot escalate an agent's mode
- All task parameters are validated with strict regex before subprocess execution; no `shell=True` anywhere

### What to expect in the dashboard

Once an agent is approved and checking in, you should see:

- **Dashboard page:** Host cards with live CPU/Memory/Disk bars, status dots, last checkin time
- **Monitor page:** Select a host to see SVG ring gauges (CPU, Memory, Disk, Load) and Chart.js time-series charts with configurable time range (1h/6h/24h/7d)
- **Alerts page:** Alerts fire automatically when metrics breach rule thresholds (7 default rules are pre-configured for CPU, memory, disk, and swap). Alerts auto-resolve when metrics recover.
- **Tasks page:** Author multistep tasks in YAML, deploy across your fleet. My Library tab for your private tasks, History tab for execution logs. Publish/Unpublish from your library to share with the community.
- **Community page:** Browse task templates published by others. Fork any template into your own library to customize and deploy.
- **Settings page:** Enrollment queue for approving/rejecting pending hosts. TOTP enrollment and management.

## Project Layout

```
Vigil/
├── docker-compose.yml
├── .env.example
├── agent/                    # Python monitoring agent
│   ├── config.example.yml
│   ├── requirements.txt
│   └── vigil_agent/
│       ├── __main__.py       # Main loop: register → checkin → collect → execute
│       ├── client.py         # HTTPS client (register, checkin, report)
│       ├── collector.py      # psutil metric collection
│       ├── config.py         # YAML config + token generation
│       ├── executor.py       # Task execution with mode/allowlist enforcement
│       ├── verify.py         # Ed25519 signature verification + TOFU key pinning
│       └── nonce_store.py    # Replay protection
└── server/
    ├── Dockerfile
    ├── requirements.txt
    ├── manage.py
    ├── vigil/                # Django project config
    │   ├── settings.py       # Production settings (Postgres, Redis)
    │   ├── settings_local.py # Local dev settings (SQLite, synchronous Celery)
    │   ├── celery.py
    │   ├── signing.py        # Ed25519 task signing
    │   └── urls.py           # URL config + dashboard view
    ├── templates/
    │   └── dashboard.html    # Full SQSY dashboard (single-page, dark theme)
    └── apps/
        ├── hosts/            # Host model, enrollment, checkin endpoint
        ├── metrics/          # MetricPoint model, metric history API
        ├── alerts/           # AlertRule, Alert, NotificationChannel, evaluation task
        ├── tasks/            # Task, TaskDefinition, TaskRun — YAML authoring + fleet deploy
        └── accounts/         # UserProfile, TOTP enrollment (RFC 6238)
```

## API Endpoints

### Agent-facing (Bearer token auth)

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/register` | Agent self-registration (creates pending host) |
| `POST` | `/api/v1/checkin` | Metric ingest + task dispatch |
| `POST` | `/api/v1/tasks/result/` | Report task execution outcome |

### Admin-facing (session or token auth)

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/hosts/` | List enrolled hosts |
| `GET` | `/api/v1/hosts/{id}/` | Host detail |
| `POST` | `/api/v1/hosts/{id}/approve/` | Approve a pending enrollment |
| `POST` | `/api/v1/hosts/{id}/reject/` | Reject a pending enrollment |
| `POST` | `/api/v1/hosts/{id}/poll/` | Request immediate check-in |
| `GET` | `/api/v1/metrics/{host}/{cat}/{metric}/` | Query metric history |
| `GET` | `/api/v1/alerts/` | List alerts (filter: `?state=firing\|acknowledged\|resolved`) |
| `POST` | `/api/v1/alerts/{id}/acknowledge/` | Acknowledge a firing alert |
| `POST` | `/api/v1/alerts/{id}/silence/` | Silence a firing alert |
| `GET` | `/api/v1/tasks/` | List tasks |
| `POST` | `/api/v1/tasks/` | Dispatch a single-action task |
| `GET` | `/api/v1/tasks/actions/` | Action registry (for editor autocomplete) |
| `GET/POST` | `/api/v1/tasks/definitions/` | List/create task definitions (YAML) |
| `POST` | `/api/v1/tasks/definitions/validate/` | Validate YAML without saving |
| `GET/PUT/DELETE` | `/api/v1/tasks/definitions/{id}/` | Read/update/delete a definition |
| `POST` | `/api/v1/tasks/definitions/{id}/fork/` | Fork a community template |
| `POST` | `/api/v1/tasks/definitions/{id}/publish/` | Publish to community |
| `POST` | `/api/v1/tasks/definitions/{id}/unpublish/` | Unpublish from community |
| `POST` | `/api/v1/tasks/definitions/{id}/deploy/` | Deploy across hosts (requires 2FA) |
| `GET` | `/api/v1/tasks/runs/{id}/` | Run detail with per-host step status |
| `GET` | `/api/v1/accounts/totp/` | TOTP enrollment status |
| `POST` | `/api/v1/accounts/totp/enroll/` | Start TOTP enrollment (returns secret + URI) |
| `POST` | `/api/v1/accounts/totp/enroll/confirm/` | Confirm enrollment with a 6-digit code |
| `POST` | `/api/v1/accounts/totp/disable/` | Disable TOTP (requires current code) |
| `GET` | `/api/v1/health/` | Health check (no auth) |

## Task Authoring & Deployment

Vigil's task system lets you author multistep tasks as YAML definitions, then deploy them across one or more hosts.

### YAML format

```yaml
name: Clear disk space
description: Reclaim disk on a host by pruning temp files and docker logs.
relevance: disk usage above 80%
risk: standard

actions:
  - id: cleanup-temp
    type: clear_temp_files
  - id: cleanup-docker
    type: clear_docker_logs
```

Each action runs sequentially per host. If a step fails, remaining steps on that host are aborted. The run tracks overall state across all target hosts (completed, partial, failed).

### Available actions

The action registry is the contract between the server (which signs tasks) and
the agent (which executes them). All 37 primitives below are defined in
`server/apps/tasks/spec.py` and mirrored in `agent/vigil_agent/executor.py`.
Running `run_command` requires `full_control` mode; everything else is
permitted under `managed` mode if it appears in the agent's allowlist.

**Service management**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `restart_service` | standard | `service_name` | — |
| `start_service` | standard | `service_name` | — |
| `stop_service` | standard | `service_name` | — |
| `reload_service` | standard | `service_name` | — |
| `enable_service` | low | `service_name` | — |
| `disable_service` | standard | `service_name` | — |
| `check_service` | low | `service_name` | `expect` |

**Container management**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `restart_container` | standard | `container_name` | — |
| `start_container` | low | `container_name` | — |
| `stop_container` | standard | `container_name` | — |
| `remove_container` | high | `container_name` | — |
| `pull_image` | low | `image` | — |
| `docker_compose_up` | standard | `compose_file` | `services` |
| `docker_compose_down` | standard | `compose_file` | — |
| `clear_docker_logs` | low | — | `container_name` |

**File and directory operations**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `write_file` | high | `path`, `content` | `mode` |
| `create_directory` | low | `path` | `owner`, `group`, `mode` |
| `delete_path` | high | `path` | `recursive` |
| `copy_file` | standard | `src`, `dest` | — |
| `move_file` | standard | `src`, `dest` | — |
| `set_permissions` | standard | `path` | `owner`, `group`, `mode` |

**Package management** (resolved via `pkg_manager.py` — apt/dnf/yum/pacman)

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `install_package` | standard | `package_name` | — |
| `remove_package` | standard | `package_name` | — |
| `update_package` | standard | `package_name` | — |
| `run_package_updates` | standard | — | `security_only` |

**System**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `clear_temp_files` | low | — | `older_than_days` |
| `execute_script` | high | `script_name` | — |
| `reboot` | high | — | `delay_seconds` |
| `run_command` | high | `command` | `timeout` |
| `set_hostname` | standard | `hostname` | — |

**Networking**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `add_firewall_rule` | high | `port`, `protocol` | `action` |
| `remove_firewall_rule` | high | `port`, `protocol` | — |

**User management**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `create_user` | high | `username` | `groups`, `shell` |
| `delete_user` | high | `username` | `remove_home` |
| `add_user_to_group` | standard | `username`, `group` | — |

**Cron**

| Action | Risk | Required params | Optional |
|---|---|---|---|
| `create_cron_job` | standard | `schedule`, `command` | `user` |
| `delete_cron_job` | standard | `pattern` | `user` |

### Deployment flow

1. Create a definition via the YAML editor (Tasks → New Task)
2. Click **Deploy** on a library card
3. Select target hosts in the deploy modal
4. Confirm with a 6-digit TOTP code (enrollment is required — there is no password fallback)
5. Steps dispatch in sequence per host — track progress in the run detail view

### Community sharing

Tasks are private by default. From your library, click **Publish** to share a task template with other users on this server. Others can **Fork** community templates into their own library. A future release will connect to an external task repository for cross-instance sharing.

## Two-Factor Authentication (TOTP)

Vigil implements RFC 6238 TOTP natively (no external dependencies). Task deployments require a 6-digit TOTP code once enrolled.

**Enrollment:**
1. Go to Settings → Two-Factor Authentication
2. Click "Enroll TOTP" — copy the secret into any authenticator app (Google Authenticator, Authy, 1Password, Bitwarden, Aegis)
3. Enter a code from the app to confirm enrollment

Enrollment is required to deploy tasks — there is no password fallback. TOTP can be disabled from Settings (requires a current code), but task deploys remain blocked until you re-enroll.

## Alerting

Vigil ships with 7 default alert rules that are created automatically on first migration:

| Rule | Metric | Threshold | Severity | Duration |
|---|---|---|---|---|
| High CPU Usage | cpu/usage_percent | > 90% | Critical | 5 min |
| Elevated CPU Usage | cpu/usage_percent | > 75% | Warning | 5 min |
| High Memory Usage | memory/usage_percent | > 90% | Critical | 2 min |
| Elevated Memory Usage | memory/usage_percent | > 80% | Warning | 2 min |
| Disk Nearly Full | disk/usage_percent | > 90% | Critical | Instant |
| Disk Usage High | disk/usage_percent | > 80% | Warning | Instant |
| High Swap Usage | memory/swap_usage_percent | > 50% | Warning | 5 min |

Alert evaluation runs every 60 seconds via Celery beat. Alerts auto-resolve when the metric drops back below the threshold. Custom rules can be created via the Django admin.

## Notifications

Configure notification channels in the Django admin under **Alerts > Notification channels**:

- **Webhook** — POST JSON payload to a URL. Optionally set a `secret` in the config for an `X-Vigil-Secret` header.
- **Email** — Send via Django's email backend. Configure `EMAIL_HOST`, `EMAIL_PORT`, etc. in your environment.

Each channel can be toggled independently for firing and resolved events.

## Agent Modes

| Mode | Metrics | Tasks |
|---|---|---|
| `monitor` | Collected and reported | Ignored entirely |
| `managed` | Collected and reported | Only allowlisted actions executed |
| `full_control` | Collected and reported | Any action executed |

The allowlist is defined in `agent.yml` and enforced locally by the agent — the server cannot override it.

## License

AGPLv3
