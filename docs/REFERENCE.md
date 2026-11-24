# Vigil reference

The full manual: install options, configuration, every task action, the API,
and the project layout. For the short tour, see the
[README](../README.md).

---

## Overview

Vigil is a monitoring system. An agent on each host sends data to a central server. The server collects metrics, checks alert rules, sends signed tasks back to the agents, and keeps a hardware inventory of your hosts. Traffic uses HTTPS. Tasks are signed with Ed25519, and each agent pins the server's key on first use.

**Key features:**
- Real-time metric collection (CPU, memory, disk, network, swap, load average, processes)
- 20 built-in alert rules with auto-resolution and host-offline detection
- Notification dispatch (webhook, email)
- Hardware inventory with OS, CPU, RAM, BIOS, MAC, uptime, timezone, and custom collector columns
- Nessus/Tenable vulnerability integration: import scan results, start scans from the UI with "Scan now", or have agents request a scan through a task action. High and critical findings raise alerts
- Active Directory computer import with auto-tagging from OU paths
- Tag-based fleet segmentation — deploy tasks by tag or by individual host
- Multistep task authoring (YAML editor) with schedule windows, retry policies, and success criteria
- Task history with pagination, refreshed every 5 seconds while the History tab is open
- Community task catalog on GitHub. Submit your YAML as a pull request from the editor, and browse approved entries
- Two-factor authentication (TOTP) for running tasks and approving hosts. Each code works once
- Signed remote tasks. The agent enforces its mode and allowlist
- Dashboard with charts

## Editions

Vigil has two editions. Free covers monitoring, alerting, and unlimited agents,
hosts, and retention. Business adds features for proving to a third party that
your systems are running, such as sites, the audit log, roles, and branding.
See [docs/EDITIONS.md](EDITIONS.md) for the full feature matrix.

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

`USE_SQLITE=true` uses SQLite instead of PostgreSQL and TimescaleDB, so you do not need those for local testing.

---

## Quick Start (Docker Compose)

```bash
cp .env.example .env
```

**1. Generate a signing key seed.** This is a 32-byte Ed25519 seed, base64-encoded. It is required, because every check-in is signed with it:

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
# Output is exactly 44 characters ending in '=' — example: Hk3PtP...P0c=
```

**2. Add it to `.env`,** together with `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD`. In a `.env` file, write the value without quotes:

```
VIGIL_SIGNING_KEY_SEED=Hk3PtP...P0c=
```

> **If you paste the compose stack into Portainer** (or another tool that rewrites YAML), set the value in the **Environment variables** tab, not in the YAML. If you must put it in the YAML, **wrap it in double quotes** (`"Hk3...P0c="`). The trailing `=` is base64 padding, and unquoted YAML can strip or change it. A malformed seed causes `binascii.Error: Incorrect padding` and stops every agent from checking in.

**3. Check the seed before you start the stack:**

```bash
python3 -c "import base64,sys; s=sys.argv[1]; print('OK',len(base64.b64decode(s)),'bytes')" \
  "$(grep '^VIGIL_SIGNING_KEY_SEED=' .env | cut -d= -f2-)"
# Expect: OK 32 bytes
```

Any other output, especially `binascii.Error: Incorrect padding` or a length other than 32, means the seed is wrong. Fix it before you continue.

**4. Start the stack:**

```bash
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

This starts Django, PostgreSQL with TimescaleDB, Redis, the Celery worker, and Celery beat.

---

## Upgrading

```bash
./scripts/vigil-backup.sh          # takes a dump first — see Backups below
docker compose pull
docker compose up -d
```

What the upgrade does:

- **Migrations run automatically** when the first container starts. Each migration is applied once.
- **Running tasks are lost.** Replacing the worker container stops any task it
  is running. Vigil marks those tasks as expired, so they do not stay
  `DISPATCHED`. You must run them again. Upgrade when no task is in progress.
- **Agents keep working during the upgrade.** They retry with backoff and
  reconnect on their own. You do not need to change them.
- **A new agent version in the image does not update your agents.** To update
  them, deploy the `update_agent` task. Before it replaces anything, it checks
  the new binary's SHA-256 against the digest in the signed task.

Downgrading is not supported, because migrations cannot be reversed. To go back,
restore a backup.

## Backups

There are three named volumes: `pgdata`, `media`, and `images`. Deleting a volume
deletes everything in it. No other copy is kept.

```bash
./scripts/vigil-backup.sh /mnt/backups     # db + media + a manifest
./scripts/vigil-restore.sh /mnt/backups/vigil-20260910T120000Z
```

The backup script does not include `images`. Those are OS install trees of several
gigabytes, and you can import them again from their source. Copying them would
make each backup too large to run often. The manifest lists the registered
images so you can download them again.

Run the backup from cron and keep a copy off the machine. A backup on the same
disk as the database is only a copy.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | _(none — required)_ | Django secret key. Vigil refuses to start without it; the compose file no longer substitutes a default |
| `DJANGO_DEBUG` | `false` | Leave it off. When on, it turns off the production configuration checks and shows detailed error pages |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | _(empty)_ | Comma-separated origins trusted for POSTs, scheme included — required behind a proxy or external hostname, else `Origin checking failed` |
| `USE_SQLITE` | _(unset)_ | Set to `true` to use SQLite instead of PostgreSQL |
| `POSTGRES_DB` | `vigil` | PostgreSQL database name |
| `POSTGRES_USER` | `vigil` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `vigil` | PostgreSQL password |
| `POSTGRES_HOST` | `db` in compose, `localhost` otherwise | PostgreSQL host. The compose file sets this itself — editing it in `.env` changes nothing |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` in compose | Redis URL. The compose file sets this itself — editing it in `.env` changes nothing |
| `VIGIL_SIGNING_KEY_SEED` | _(empty)_ | Base64 Ed25519 seed — required for task deployment |
| `VIGIL_TIMEZONE` | `UTC` | IANA timezone for schedule window evaluation (e.g. `America/New_York`) |
| `VIGIL_METRIC_RETENTION_DAYS` | `30` | Days of raw metric history to keep |
| `VIGIL_ALERT_RETENTION_DAYS` | `90` | Days to keep *resolved* alerts. Firing and acknowledged alerts are never pruned. `0` disables pruning |
| `VIGIL_MAX_REQUEST_BODY_BYTES` | `8388608` (8 MB) | Largest request body Vigil accepts. Task results can be large, such as a Trivy scan report. If a result is larger than this limit, the request fails with a plain `400` error, and the task stays `DISPATCHED` |
| `VIGIL_AGENT_VERSION` | _(ignored)_ | Not used. The expected agent version comes from the agent bundled with the server. If you set it, the server logs a note at startup and continues |
| `NESSUS_URL` | _(empty)_ | Nessus/Tenable server URL |
| `NESSUS_ACCESS_KEY` | _(empty)_ | Nessus API access key |
| `NESSUS_SECRET_KEY` | _(empty)_ | Nessus API secret key |
| `NESSUS_VERIFY_SSL` | `true` | Verify Nessus TLS certificate |
| `EMAIL_BACKEND` | `console` | Django email backend |
| `EMAIL_HOST` | `localhost` | SMTP host |
| `EMAIL_PORT` | `587` | SMTP port |
| `VIGIL_NOTIFICATION_FROM_EMAIL` | `vigil@localhost` | From address for alert emails |
| `VIGIL_PUBLIC_URL` | — | External URL for remote access. Its host and origin are added to `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` automatically |
| `VIGIL_TRUST_PROXY` | `false` | Trust `X-Forwarded-Proto` and `Host` headers from a proxy or tunnel that ends TLS |
| `TUNNEL_TOKEN` | — | Cloudflare Tunnel token for `docker compose --profile tunnel up` |

---

## Remote Access (off-LAN check-in)

Agents only make outbound connections, so you reach Vigil from outside your
network by exposing the server at a public or overlay address. Set `VIGIL_PUBLIC_URL` to
your external URL and `VIGIL_TRUST_PROXY=true` when a proxy terminates TLS. The
repo ships a `cloudflared` sidecar (`docker compose --profile tunnel up -d`) for
Cloudflare Tunnel. Full recipes for **Cloudflare Tunnel**, **Tailscale**, and a
generic **reverse proxy** are in [docs/REMOTE-ACCESS.md](REMOTE-ACCESS.md).

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

> **Monitor mode runs without privileges**, on Windows and Linux. On Linux it runs
> as `vigil-agent` with systemd hardening. On Windows it runs as the
> `NT SERVICE\vigil-agent` virtual account. To switch to `managed` or
> `full_control`, change one line in the config and run the installer again. See
> [docs/AGENT-PRIVILEGES.md](AGENT-PRIVILEGES.md) for what each mode can and
> cannot do. In monitor mode, a Windows agent cannot report Windows Update
> status, and cannot prepare a rebuild.

Run the agent:

```bash
python3 -m vigil_agent -c agent.yml --log-level DEBUG
```

On the first run, the agent:
- Generates a token and saves it to `agent.yml`
- Registers with the server, which creates a **pending** host
- Starts checking in. The server replies with `{"status": "pending"}` until you approve the host

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
- Run `chmod 600 agent.yml`. The config file contains the agent token.
- **Server key pinning:** the agent saves the first public key it receives from the server to `data_dir/server_public_key.pin`. If the key ever changes, the agent rejects all tasks until you delete the pin file by hand. Delete it only after you confirm the change is expected.
- **The agent enforces its own mode.** A compromised server cannot raise an agent's mode or allowlist.
- The agent does not run commands with `shell=True`. It checks every task parameter before it runs the task.

---

## Dashboard Pages

| Page | Description |
|---|---|
| **Dashboard** | A grid of widgets that you can arrange, resize, and rename. Add widgets from the catalog, which is grouped by purpose. You can keep several dashboards and switch between them. The **Host status** widget shows a card for each host: a status dot, the OS logo, tags, the mode badge, live CPU, memory, disk, and network bars, and the RDP, Deploy, and Remove buttons. Click a card to open the host details. |
| **Inventory** | A hardware table for all enrolled hosts. Columns include hostname, IP, OS, CPU, RAM, MAC, BIOS, disks, uptime, last user, and timezone. Click a column header to sort. Filter each column with `=value` for an exact match, or type text for a partial match. Drag columns to reorder them. Use the Columns button to show or hide columns. |
| **Tasks** | The YAML task editor, with a **Submit to Community** button that opens a GitHub pull request, and your private library. The **History** tab lists every task sent to hosts. It updates live, shows the newest first, and is paginated. |
| **Vulns** | Nessus/Tenable scan findings for each host, with a **Scan now** button on each row. The **Recent scans** section lists every scan request, whether it came from the UI or an agent, and its status. |
| **Firewall** | Firewall rules and default policies for each host. Vigil reads them when you open the tab. See [Firewall management](#firewall-management). |
| **Monitor** | Select a host to see live gauges for CPU, memory, disk, and load, and charts over time. Choose a range of 1 hour, 6 hours, 24 hours, or 7 days. Windows hosts have an RDP download button. |
| **Alerts** | Firing, acknowledged, and resolved alerts across the fleet. |
| **Community** | Browse forks of approved community tasks. Submissions are stored on GitHub at [SusquehannaSyntax/Vigil-Approved-Scripts](https://github.com/SusquehannaSyntax/Vigil-Approved-Scripts). To submit a task, use **Submit to Community** in the task editor. |
| **Settings** | The enrollment queue (approve or reject pending hosts), TOTP setup, Active Directory import, and the server time zone. |

---

## Hardware Inventory

Agents send a hardware snapshot every hour. Metrics are sent every 60 seconds. The inventory page shows:

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

Tags are text labels on hosts. Use them to deploy tasks to groups of hosts.

**Where tags come from, in order:**
1. `agent.yml`: `tags: [web-server, prod, rack-3]`. The agent sends these at each check-in.
2. Server tags, which you edit in the host details panel (click any host card).
3. Automatic tags, set at check-in from the OS (`linux`, `windows`, `macos`) and the mode (`managed`, `monitor`, `full_control`).
4. Active Directory import, which uses the OU path. For example, `OU=Servers,OU=IT` gives the tags `servers` and `it`.
5. Tasks, with the `add_tag` and `remove_tag` actions. A task or baseline can tag a host it has just changed.
6. Install profiles. A rebuilt host gets the profile's tags when it checks in.

**Deploy by tag.** In the deploy window, switch the target from "Individual Hosts" to "By Tag". The task goes to all online managed hosts with that tag.

**OS logos** are stored under `server/static/img/os/`. They are based on
[Simple Icons](https://simpleicons.org) (CC0-1.0), placed on a colored disc.
The Windows and Bazzite logos were drawn by hand. Vigil serves the logos itself.
They are not loaded from the internet, so the console works on a network
without internet access.

**The `agent:` prefix is reserved.** Tags that an agent reports about itself use
this prefix. Tasks, install profiles, and the API cannot write tags with this
prefix. This stops an agent from giving itself a tag that your baselines or
deploy rules use.

---

## Task Authoring

Tasks are written as YAML in the built-in editor (Tasks > New Task). You deploy
them to hosts from the deploy window.

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

# Optional: validate step output (supports ${{ inputs.x }} variables)
success_criteria:
  exit_code: 0
  output_contains: "active (running)"   # substring match
  output_regex: "^OK"                   # regex (applied after output_contains)

# Optional: write this task's output into the host's inventory as a custom column
collect:
  column: nginx_version           # required — the custom_columns key (≤ 80 chars)
  parse: output_line_1            # optional — output_line_1 (default) | output_trim | output_full

actions:
  - id: reload
    type: reload_service
    params:
      service_name: "${{ inputs.service }}"
    success_criteria:
      exit_code: 0
      output_contains: "${{ inputs.service }} reloaded"

  - id: verify
    type: check_service
    params:
      service_name: "${{ inputs.service }}"
      expect: active

  # Optional per-step keys
  - id: build
    type: run_command
    when: 'inputs.run_build == "yes"'   # skip this step unless it matches
    timeout: 1800                       # seconds, 1–3600 (default 120)
    params:
      command: "make -j8 all"
```

**`when:`** gates a single step on a predicate over `agent.*` (platform facts:
`os`, `arch`, `pkg_manager`, `hostname`) and `inputs.*` (the values supplied at
deploy time). A step whose predicate is false is skipped and recorded, and does
not block later steps. Referencing an input you did not declare is rejected when
the task is saved — otherwise the step would silently never run.

**`timeout:`** sets the time limit for a step, in seconds. The default is 120 seconds, and the maximum is 3600 seconds (one hour). Use it for long steps such as source builds, large image pulls, and scans of large file systems.

**Schedule windows** use the server's `VIGIL_TIMEZONE`. A task that is outside its window stays `PENDING`. It is sent at the next check-in that falls inside the window.

**Retry.** If a step fails, the agent runs it again after `delay_seconds`, up to `attempts` times. If every attempt fails, the task is marked as failed.

**Success criteria.** A step with exit code 0 still fails if `output_contains` or `output_regex` does not match. Criteria on a step replace the top-level criteria.

**Collect.** A task with a top-level `collect:` block saves its output as a column on the Inventory page. When the run finishes on a host, the output is saved for that host, and the column appears on the Inventory page. The block is a **single mapping**. One task fills one column. It keys are:

| Key | Required | Meaning |
|---|---|---|
| `column` | yes | The `custom_columns` key to write. String, ≤ 80 characters. |
| `parse` | no | How the task output becomes the stored value. One of `output_line_1` (default), `output_trim`, `output_full`. |

There is **no `value:` key and no `{{ steps.* }}` templating**. The value always comes from the task's own output, changed by `parse`:

- `output_line_1` *(default)* — the first non-empty line of output, with a leading `[OK]` step prefix and any `label:` prefix stripped; truncated to 500 characters. Best for a task whose script echoes a single value.
- `output_trim` — the entire output, whitespace-trimmed, truncated to 500 characters.
- `output_full` — the entire output verbatim, truncated to 2000 characters.

The column is written for each host when its run finishes. If you run a `collect:` task against a tag or the whole fleet, the column fills for every targeted host. Example: record each host's kernel version in a `kernel` column:

```yaml
name: Record kernel version
description: Capture uname -r into the host inventory.
risk: low
collect:
  column: kernel
  parse: output_line_1
actions:
  - id: uname
    type: run_command
    params:
      command: "uname -r"
```

### Available actions

Vigil has 53 actions. `run_command` and `execute_script` require the `full_control` mode. All other actions require `managed` or `full_control`.

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
| `recreate_container` | `container_name` | `image` |
| `docker_compose_up` | `compose_file` | `services` |
| `docker_compose_down` | `compose_file` | — |
| `clear_docker_logs` | — | `container_name` |
| `check_docker_updates` | — | — |

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
| `add_firewall_rule` | `port`, `protocol` | `action`, `source`, `interface` |
| `remove_firewall_rule` | `port`, `protocol` | `action`, `source` |
| `list_firewall_rules` (low risk — read-only) | — | — |
| `set_firewall_policy` | `direction`, `policy` | — |
| `enable_firewall` | — | — |
| `disable_firewall` | — | — |

`list_firewall_rules` is the only low-risk action in this group. It changes nothing, so the Firewall tab sends it freely. The other five are high-risk. See [Firewall management](#firewall-management).

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

**Vulnerability scanning**

| Action | Params | Optional |
|---|---|---|
| `request_nessus_scan` | — | — |
| `request_network_scan` | — | `engine` (`nessus` \| `greenbone`) |
| `run_trivy_scan` | — | `scope` (`fs` \| `rootfs` \| `image:<name>`) |
| `trivy_db_update` | — | — |

The two `request_*` actions only leave a marker: the agent finishes, and the server creates a `VulnScan` for the central scanner to run. `run_trivy_scan` is the opposite — the scan *is* the task, and the findings arrive with its output. See [Vulnerability Management](#vulnerability-management).

**Host tagging** *(needs an agent version that supports tagging; update your agents first)*

| Action | Params | Optional |
|---|---|---|
| `add_tag` | `tags` | — |
| `remove_tag` | `tags` | — |

`tags` is a **comma-separated string**, not a list:

```yaml
actions:
  - type: add_tag
    when: has_docker            # evaluated on the host, so tagging is conditional
    params:
      tags: "role:docker, env:lab"
```

Like the `request_*` scan actions, these actions only leave a marker. The server
applies the change when the result comes back. The server uses the tags in the
signed task, **not the tags the agent reports back**. An agent can only confirm a
tag that is already in the task. The reserved `agent:` prefix is refused when
you save the task and again when the tags are applied.

**Agent & baselines**

| Action | Params | Optional |
|---|---|---|
| `baseline` | `name` | — |
| `update_agent` | — | `platform` |

`update_agent` replaces the agent program. The server writes the SHA-256 of each platform's binary into the signed task. An HTTPS connection alone is not enough to check the new program.

**Reprovisioning** (see [Reprovisioning](reprovisioning.md))

| Action | Params | Optional |
|---|---|---|
| `reprovision_preflight` | — | `disk_target`, `os_family` |
| `reprovision_stage` | `job_id`, `kernel_url`, `initrd_url`, `kernel_sha256`, `initrd_sha256` | — |
| `reprovision_commit` | `job_id`, `cmdline` | — |
| `reprovision_cleanup` | `job_id` | — |

All of these except `reprovision_preflight` are high-risk. They also need `allow_reprovision: true` in the agent's `agent.yml`. A host that has not set this flag cannot be wiped by the server.

### Deployment flow

1. Write a task in the YAML editor (Tasks > New Task).
2. Click **Deploy** on a card in the library.
3. Enter any values on the **Inputs** tab.
4. If you need them, set a schedule window, retry policy, and success criteria on their tabs.
5. Choose the target hosts, or a tag, on the **Hosts** tab.
6. Enter your 6-digit TOTP code and submit.
7. Follow the run in the run details (Tasks > History). The list refreshes every 5 seconds while it is open.

---

## Community Catalog

The community catalog is on GitHub at [SusquehannaSyntax/Vigil-Approved-Scripts](https://github.com/SusquehannaSyntax/Vigil-Approved-Scripts). Any Vigil install can browse approved entries. SQSY maintainers review every submission.

### Submitting your task

The task editor's toolbar has a **Submit to Community** button. Clicking it:

1. Vigil reads the YAML in the editor and turns the task name into a file name, such as `restart-nginx-and-verify.yaml`.
2. Vigil adds `author: <your-vigil-username>` and `created: <today>` after the `name:` line, if they are not already there. The community repository requires both fields.
3. A window opens with two buttons: **Copy YAML** and **Open GitHub PR**.
4. The **Open GitHub PR** link opens GitHub's new-file editor with your YAML filled in.
5. If you do not have write access to the repository, GitHub makes a fork for you. Click **Propose new file**, then **Create pull request**.
6. A SQSY maintainer reviews and merges the pull request. After it is merged, every Vigil install can see the entry.

### Required YAML fields for community submissions

| Field | Type | Auto-filled by Submit-to-Community |
|---|---|---|
| `name` | string | No (must be in your YAML) |
| `description` | string | No |
| `risk` | `low` / `standard` / `high` | No |
| `actions` | non-empty list | No |
| `author` | string (your Vigil username) | **Yes** |
| `created` | ISO-8601 date (`YYYY-MM-DD`) | **Yes** |

`author` and `created` are optional for private tasks. When they are present, they appear on the task card and in the editor preview. If a task declares an `author`, that name is shown instead of the local owner, so a forked task keeps its original author.

Submissions go through GitHub. Vigil does not host its own community list.

---

## Automations — event filters

An event automation runs when an alert is raised. Besides severity and alert rule,
these filters limit which alerts count:

| Filter | Effect |
|---|---|
| **Only for events on host** | Fires only for alerts on that one host |
| **Only when the alert's name/description contains…** | Substring match, case-insensitive |
| **…does not contain** | Fires only when the text is absent |

The alert's **name** is the name of its rule. Its **description** is the alert
text. The filters check the name or the description. The *does not contain*
filter fails if the text appears in either one.

These filters choose which alerts trigger the automation. They do not choose
where it runs. An automation can watch one host and act on another, which is why
"Only for events on host" is separate from "Run on". An alert that Vigil raises
directly, instead of from a rule, has no name. *Contains* never matches it, and
*does not contain* passes it.

---

## Alerting

Vigil has **20 default alert rules**, created when the database is first set up. Rules are checked every 60 seconds. An alert resolves automatically when its metric drops below the threshold.

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

**Host offline.** When a host misses check-ins for 5 minutes or more, Vigil creates an alert. The alert resolves at the next successful check-in.

You can create your own rules in the Django admin.

---

## Notifications

Set up notification channels in the Django admin, under **Alerts > Notification channels**:

- **Webhook.** Vigil sends a JSON message by POST to a URL. To add a signature check, set a `secret` in the config. Vigil then sends it in the `X-Vigil-Secret` header.
- **Email.** Vigil sends email through Django's email backend. Set `EMAIL_HOST`, `EMAIL_PORT`, and the other email settings in your `.env` file.

---

## Two-Factor Authentication (TOTP)

Vigil uses time-based one-time passwords (TOTP, RFC 6238). After you set up TOTP,
deploying a task requires a 6-digit code.

**Set up TOTP:**
1. Go to Settings > Two-Factor Authentication.
2. Click **Enroll TOTP**. Copy the secret into an authenticator app, such as Google Authenticator, Authy, 1Password, Bitwarden, or Aegis.
3. Enter a code from the app to confirm.

You cannot deploy tasks until TOTP is set up. To turn TOTP off, go to Settings. You need a current code to do so.

### High-risk steps in a baseline

A baseline runs on its own, on every host that matches it when the host is
enrolled. Nobody is there to enter a TOTP code. For that reason, baselines do
not accept high-risk steps by default.

To allow high-risk steps, tick **Allow high-risk steps in this baseline**. This
is off by default. When you tick it, you enter a new TOTP code. That code
approves every later run of this baseline. Clearing the box needs no code.
Duplicating a baseline does not copy this setting. You must tick it again on the
copy.

**`update_agent` is never allowed in a baseline**, even with this setting on. Run it by hand, so you can check the SHA-256 digest.

---

## Vulnerability Management

Vigil collects findings from three scanners and shows them on one Vulnerabilities tab, with one score per host. Set up whichever scanners you use. None are required. Vigil skips a scanner that is not set up.

| Scanner | Where it runs | How Vigil gets findings | Setup |
|---|---|---|---|
| **Nessus / Tenable** | Central server you run | Vigil launches scans over the REST API and polls | `NESSUS_*` env vars |
| **Greenbone / OpenVAS** | Container you run | Vigil launches scans over GMP (XML/TLS) and polls | `GREENBONE_*` env vars |
| **Trivy** | The monitored host itself | The scan *is* an agent task; findings arrive with its output | Nothing server-side |

Every hour, Vigil checks each configured network scanner. If one scanner fails,
the others still run. Trivy works differently. Its findings arrive when its task finishes.

> `vulns.sync_nessus_vulns` is an old name that still works for now. It will be removed. Use `vulns.sync_vulns` instead.

### Trivy — agent-local, no server setup

Trivy scans the host's own file system and installed packages against a CVE
database. It does not open a network port, and you do not need a central scanner.
Deploy a task that uses the `run_trivy_scan` action:

```yaml
name: Trivy filesystem scan
risk: low
actions:
  - id: scan
    type: run_trivy_scan
    params: { scope: fs }      # or image:<name> for a container image
```

The agent needs the `trivy` program. Use the **Install Trivy on this host** task template, or install it yourself.

> **`managed` agents must allow the action.** The default `agent.yml` allowlist does *not* include `run_trivy_scan`. A managed agent rejects the task, and no findings arrive. Add `run_trivy_scan` to the host's `allowlist:`. Add `trivy_db_update` too if you update the database yourself. `full_control` agents ignore the allowlist.

When the scan finishes, the server records one finding for each package and CVE pair. Earlier open findings that did not appear again are marked `FIXED`. The host score is updated. The result is added to the task output. The run details show either `[INGEST] trivy: N finding(s) ingested` or `[INGEST FAILED] <reason>`.

**Report size.** A full `trivy fs /` report can be about 30 MB on a typical Ubuntu workstation. About 90 percent of it is a package list that Vigil does not use. The agent keeps only the fields that Vigil needs. It removes duplicate package and CVE pairs, and compresses the result. In one test, a 30 MB report was reduced to 22.9 KB, with every finding kept. A host with thousands of findings still fits in one request. Keep these points in mind:

- Agents older than **2026.6.2** send the full report. Vigil cannot read it, because it is cut off in transit. The Vulnerabilities tab stays empty, and the run details say the report is truncated. Update the agent.
- If a proxy sits in front of Vigil, its request size limit must be at least `VIGIL_MAX_REQUEST_BODY_BYTES` (8 MB by default). Otherwise the proxy rejects the results before Vigil receives them.
- Vigil reads both compressed and uncompressed reports. Error messages show readable text, not base64.

### Suggest Fix

Every finding on the Vulnerabilities tab has a **Suggest Fix** button. When the scanner reports a package and a fixed version, which is always the case for Trivy, the button fills in an `update_package` task. This does not use an AI model:

```yaml
name: "Upgrade openssl to 3.0.3"
description: "Remediates CVE-2024-0001 on web-01 (installed 3.0.2 -> fixed 3.0.3)"
risk: standard
actions:
  - type: update_package
    params:
      package_name: "openssl"
```

The suggestion applies to the **package**, not the single CVE. One upgrade fixes every CVE open against that package. The suggestion states how many CVEs it covers.

If you have set up an AI provider, it also runs for cases that the task cannot cover. These are findings with no fixed version, or findings with no package, which is common for Nessus network findings. The AI asks for a diagnostic instead of an upgrade. Vigil checks every AI suggestion before showing it. It removes `update_agent` from any suggestion. Nothing runs until you deploy it.

**A report with no `Vulnerabilities` section is rejected.** That report means the vulnerability scan did not run. If Vigil accepted it, every existing finding would be marked fixed, and the host would look clean. Vigil leaves the existing findings as they are, and the reason appears in the run details.

### Nessus setup

1. Install **Nessus Essentials**. It is free, and it scans up to 16 IP addresses. Register at <https://www.tenable.com/products/nessus/nessus-essentials> to get an activation code. Then run:

   ```bash
   curl -o nessus.deb 'https://www.tenable.com/downloads/api/v2/pages/nessus/files/Nessus-latest-debian10_amd64.deb'
   sudo dpkg -i nessus.deb
   sudo systemctl enable --now nessusd
   ```

   Open `https://localhost:8834` and complete activation. Plugin compilation takes about 20 to 30 minutes. Create API keys under **My Account > API Keys > Generate**.

2. Wire the keys into Vigil's `.env`:

   ```env
   NESSUS_URL=https://localhost:8834
   NESSUS_ACCESS_KEY=<paste>
   NESSUS_SECRET_KEY=<paste>
   NESSUS_VERIFY_SSL=false        # self-signed cert by default
   ```

   If Vigil runs in Docker and Nessus is installed on the host, use `https://host.docker.internal:8834` on a Mac, or `https://172.17.0.1:8834` on Linux.

3. Verify the connection:

   ```bash
   docker compose exec server python manage.py shell -c \
     "from apps.vulns.tasks import sync_nessus_vulns; print(sync_nessus_vulns())"
   ```

### What runs automatically

Every hour, `vulns.sync_vulns` does three things for each configured network scanner:

1. **Starts** each scan in the `requested` state, using Nessus's `Basic Network Scan` template against the host's IP address. For Greenbone, it uses the equivalent template.
2. **Checks** scans that are running and updates their status on the dashboard.
3. **Imports** results from finished scans. It raises alerts for new critical and high findings, and resolves them when the findings are gone.

### Launching scans from Vigil

There are three ways to start a scan. Each one adds a scan to the **Recent scans** list:

| Path | Who triggers | TOTP gate |
|---|---|---|
| **Scan now** button (Vulnerabilities tab) | Operator from UI | Yes |
| **Request a Nessus scan** task template | Operator dispatches to a host | Yes (at deploy time) |
| `request_nessus_scan` action in a custom YAML task | Anyone with a published YAML using this action | Yes (deploy gate) |

A host can have only one active scan. If you request another while a scan is running, Vigil returns `409 Conflict`.

### Ready-made templates in the editor

The task editor's **Start from template…** dropdown covers all three scanners:

| Template | Risk | What it does |
|---|---|---|
| **Install Nessus Essentials on this host** | high | Downloads, installs and starts `nessusd`, then echoes the activation URL |
| **Request a Nessus scan of this host** | low | `request_nessus_scan` — the server picks up the marker on completion and queues a scan |
| **Install Greenbone Community Edition** | high | Drops the official CE compose stack into `/opt/greenbone` and brings it up (Linux, docker-capable hosts only) |
| **Request a network scan of this host** | low | `request_network_scan` — engine-agnostic; the server picks Nessus or Greenbone |
| **Install Trivy on this host** | high | Cross-platform install via apt / dnf / brew / winget, chosen by per-step `when:` predicates |
| **Run a Trivy vulnerability scan** | low | `run_trivy_scan` — the scan runs on the host and the findings come back with the task |

---

## Firewall Management

The **Firewall** tab (between Vulns and Monitor) shows and edits the `ufw`, `firewalld`, or Windows Firewall settings on a host. You can change the rules, each rule's source and interface, and the default incoming and outgoing policies.

### Fetch-on-demand, not polled

Firewall settings are not read with the regular metrics. When you select a host, Vigil sends a `list_firewall_rules` task. The result arrives at the host's *next* check-in. **The first time you open a host, the tab shows "Nothing read yet". This is expected.** Click Refresh, or wait for the next check-in, and the tab fills in. After you change a rule, the tab does not read the firewall again. Click Refresh to confirm the change.

### Three changes the tab refuses

The Firewall tab refuses three kinds of change. Vigil refuses them before it asks for a TOTP code:

1. **Setting the default outgoing policy to `deny` or `reject`.** Agents connect outward to the server, so this cuts the agent off. The host stops checking in, and no task can be sent to undo the change. Recovery needs console access to the host.
2. **Denying the remote-access port, or removing the rule that allows it** — port 22 always, plus 3389 on Windows.
3. **Setting the default incoming policy to `deny`** when no allow rule covers the remote-access port.

`disable_firewall` is **not** refused. It opens the host rather than closing it. It needs a TOTP code, like any other high-risk task.

**The task editor does not apply these checks.** The checks apply only to the Firewall tab's save action (`POST /api/v1/hosts/{id}/firewall/apply/`). A task you write in the YAML editor that uses `set_firewall_policy`, `add_firewall_rule`, or `remove_firewall_rule` is not checked. Use the task editor only if you understand the change.

**Known limitation:** the protected ports are 22 and 3389, and they are fixed in the code. Vigil does not read the actual SSH port from the host. A host that runs SSH on a different port is **not** protected. A change that denies incoming traffic can lock you out of that host.

### `managed`-mode agents must allowlist the actions

The default `agent.yml` allowlist does not include the six firewall actions. On a `managed` agent, add them to the allowlist. Until you do, every firewall task is rejected, including the read. The Firewall tab then shows nothing for that host, and the screen gives no clear error. `full_control` agents ignore the allowlist. See `agent/config.example.yml` for the commented-out block.

### Rules Vigil couldn't parse

Some rules do not fit Vigil's port, protocol, and source model. Examples are ufw application profile rules (`ufw allow OpenSSH`), firewalld port ranges, and Windows rules with a port that is not a number. Vigil does not drop these rules. They appear in an "Unparsed rules" section, as plain text. You cannot edit them in Vigil. If a host allows SSH only through an application profile, Vigil does not see port 22 in the parsed rules. The lockout check then treats SSH as not allowed, and it refuses a change that denies incoming traffic by default. This is a cautious result, even if the host is fine.

---

## Active Directory Import

Set up Active Directory in **Settings > Active Directory**:

- LDAP server URL, bind DN, bind password, base DN, and the OU containing computer objects
- **Import Now** asks LDAP for computer objects. It creates a `PENDING` host for each computer that is not yet enrolled, and tags it from its OU path. For example, `OU=Servers,OU=IT` gives the tags `servers` and `it`.

---

## Agent Modes

| Mode | Metrics | Tasks |
|---|---|---|
| `monitor` | Collected | Ignored entirely |
| `managed` | Collected | Only allowlisted actions |
| `full_control` | Collected | Any action |

The allowlist is set in `agent.yml`. The agent enforces it locally, and the server cannot change it.

Rebuilding a machine with [reprovisioning](reprovisioning.md) is not covered by
this table. `full_control` does not allow it, and you cannot add it to the
allowlist. It needs its own setting:

```yaml
allow_reprovision: true   # default false, everywhere
```

A compromised server cannot order machines to rebuild, because the permission is set on each machine.

---

## Remote Reprovisioning

Start a rebuild from **Reprovision > Rebuild jobs > Rebuild a host**, or from
the host's details panel. Both open the same confirmation.

A rebuild installs a new operating system on a host from the console. You choose
an image and a profile. Then you confirm with your password, an authenticator
code, and the hostname. The machine wipes its disk, installs unattended, and
reconnects under the same host record. It gets a tag you choose. You can also
run a baseline afterward. This returns a drifted or compromised machine to a
known-good state without a site visit.

Supported systems are Ubuntu, Debian, and the RHEL family. This feature is free.

**A rebuild erases all data on the target disk. It cannot be undone once the
installer starts.** Read [the reprovisioning guide](reprovisioning.md) before
you start a rebuild. It explains the limits, including that a rebuild does not
remove malware that has kernel-level access.

### OS image library

The **Reprovision** section manages the images that a rebuild installs from. Vigil downloads each image once and serves it to every host that rebuilds from it. A rebuild needs a route only to Vigil, not to the internet.

Vigil includes a short list of distributions that it can download by itself:

| Catalog entry | Family | Size |
|---|---|---|
| Ubuntu Server 26.04 LTS | `ubuntu` | 2.9 GB |
| Ubuntu Desktop 26.04 LTS | `ubuntu` | 6.5 GB |
| Ubuntu Server 24.04 LTS | `ubuntu` | 3.4 GB |
| Ubuntu Server 22.04 LTS | `ubuntu` | 2.1 GB |
| Linux Mint 22.3 Cinnamon | `ubuntu` | 3.1 GB |
| Debian 13 (netinst) | `debian` | 0.8 GB |
| Fedora Server 43 | `rhel` | 3.5 GB |
| Fedora Workstation 43 | `rhel` | 2.7 GB |
| Bazzite (stable) | `rhel` | 7.9 GB |
| Rocky Linux 9 (minimal) | `rhel` | 2.8 GB |
| AlmaLinux 9 (minimal) | `rhel` | 2.8 GB |

The **Add an image** tab shows each distribution as a card. Images already in
your library are greyed out. Vigil never downloads the same file twice. If you
pull an image whose checksum is already in the library, Vigil returns a 409
error. If an earlier download failed, pulling it again retries that entry.

Importing an image needs free space for the ISO and the extracted files at
the same time. That is about twice the sizes listed above, for a short time. After
Vigil deletes the ISO, the space needed drops to about the size listed. Import
runs on one CPU core, so a low-power host can take a long time to import an
image. You import each image once, not once per rebuild.

Each catalog entry has a public download URL and a published SHA-256 checksum. The list is updated with each Vigil release. To use a newer release before it is added, enter a custom URL and its SHA-256 checksum.

**RHEL and Windows are not in the catalog.** RHEL needs a Red Hat subscription to download, so it has no public URL. Microsoft does not publish a stable download with a checksum. To add these images, upload an ISO you are licensed to use, or enter a URL and its SHA-256 checksum. **A SHA-256 checksum is always required.** Catalog entries include it. For a custom URL or an upload, you must enter it. An image without one is refused before any data is transferred.

**Windows images cannot be imported.** The upload and pull buttons for Windows are disabled. Vigil imports Linux images, which use a `vmlinuz` kernel and an `initrd`. Windows uses a different boot method, so Vigil does not support it.

### Install profiles

An install profile holds the answers the installer needs to run unattended. It
sets the image and disk, the partitions and file system, the network address,
the locale, and who can log in afterward. Go to **Reprovision > Install profiles > + New profile**.

The form checks two things, because a mistake would only show up after the disk
is wiped:

- **Someone must be able to log in.** A profile needs an SSH key or an admin password hash. The password field takes a *crypt hash*, made with `mkpasswd -m sha-512`. It does not take a plain password.
- **A static address needs an address and a gateway.** These fields appear only when you choose Static.

When you edit a profile, leave the password box blank to keep the stored
password. Vigil never shows a stored password again, even to the admin who set it.

**Completion tags** apply tags when the rebuilt machine checks in. Baselines and alert rules that use those tags then apply to the machine without a manual step.

### The fetch guard

Before Vigil downloads any URL, catalog or custom, it checks the address the URL resolves to. It checks again after each redirect. Vigil refuses these addresses:

- Loopback: `127.0.0.0/8` and `::1`
- Unspecified: `0.0.0.0` and `::`
- Link-local: `169.254.0.0/16` and `fe80::/10`. This range includes the instance metadata service on AWS and Azure.
- Metadata addresses for other clouds: `metadata.google.internal`, `metadata.goog`, `100.100.100.200` (Alibaba), and the AWS IPv6 metadata address.

If Vigil runs in a cloud, its metadata service can give out credentials for the whole account. Vigil must not be used to fetch from that service.

**Private network addresses are allowed.** An internal mirror is a valid image source, so RFC 1918 addresses are not refused. For a custom URL, Vigil shows a warning that it will download from your network, not from your browser. The warning does not block the download.

### Upload limits Vigil doesn't control

An ISO upload goes through any proxy in front of Vigil. **Cloudflare Tunnel** limits request bodies to 100 MB on free plans. **nginx** limits them to 1 MB by default (`client_max_body_size`). If a limit is smaller than the ISO, the upload fails, and Vigil shows no reason. Raise the limit on the proxy. Or use the URL pull instead, which does not upload through your browser.

The server is set up for large uploads in two ways:

- The gunicorn timeout in `server/Dockerfile` is `--timeout 1800`, which gives an upload 30 minutes. The default of 30 seconds would stop a large upload.
- `FILE_UPLOAD_TEMP_DIR` in `server/vigil/settings.py` stores uploads on the `VIGIL_IMAGE_ROOT` volume. Without it, large uploads would fill the container's disk.

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
| `GET` | `/api/v1/metrics/catalog/` | The category/metric pairs this fleet is actually reporting |

Categories the agent sends: `cpu`, `memory`, `disk`, `network`, `process`,
`temperature`, and `gpu`.

**Processes.** The agent reports the ten processes that use the most CPU and the
ten that use the most memory. Each is labelled with `name`, `pid`, and `rank`.
Names listed under `process_watch` in `agent.yml` are sampled at every check-in,
whatever their rank. They are labelled `watched=1`. Processes with the same name
are added together, and the count of running processes is reported as
`process/instances`. A watched name that is not running reports zero.

**GPUs.** The agent reads GPU data from `nvidia-smi` and `rocm-smi` when either
is on the PATH. If neither is found, it skips GPU data, and you need no setup.
Each GPU is labelled with `vendor`, `index`, and `name`. The values are
`utilization_percent`, `memory_used_mb`, `memory_total_mb`, `memory_percent`,
`temperature_celsius`, and `power_watts`. Set `gpu_extended: true` to add
`fan_percent`, `clock_graphics_mhz`, `clock_memory_mhz`,
`memory_bandwidth_percent`, `pcie_generation`, `pcie_width`, and ECC counters,
where the card reports them. This roughly doubles the data points per GPU at
each check-in, so use it with care on hosts with many GPUs.

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
| `POST` | `/api/v1/tasks/definitions/{id}/deploy/` | Deploy across hosts (requires TOTP) |
| `GET` | `/api/v1/tasks/history/?page=N` | Paginated task history feed (50/page, polled by the History tab) |
| `GET` | `/api/v1/tasks/runs/{id}/` | Run detail with per-host step status |

> The single-action dispatch endpoint (`POST /api/v1/tasks/`) has been removed, because it skipped the TOTP check. Use the definition deploy endpoint instead. The community catalog is on GitHub. See [Community catalog](#community-catalog).

### Reprovisioning — image library

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/reprovision/catalog/` | The distros Vigil can fetch on its own (`view`) |
| `POST` | `/api/v1/reprovision/images/pull/` | Queue a download — from `catalog_id`, or a custom `url` + `sha256` (admin only) |
| `POST` | `/api/v1/reprovision/images/upload/` | Upload an ISO directly, synchronously verified and imported (admin only) |
| `GET` `POST` | `/api/v1/reprovision/images/` | List the library (`view`) / register an image (admin only) |
| `GET` `DELETE` | `/api/v1/reprovision/images/{id}/` | Image detail (`view`) / remove it (admin only) |
| `GET` `POST` | `/api/v1/reprovision/profiles/` | List install profiles (`view`) / create one (admin only) |
| `GET` `PATCH` `DELETE` | `/api/v1/reprovision/profiles/{id}/` | Profile detail (`view`) / edit or remove (admin only) |
| `POST` | `/api/v1/reprovision/profiles/{id}/preview/` | Render the answer file this profile would produce, without running anything |

A pull of an image already in the library returns **409** and names the existing
image. If the last attempt for that image failed, the pull retries on the same
entry instead of creating a duplicate.

For the rebuild job endpoints, see [Reprovisioning](reprovisioning.md#api-reference).

### Misc

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/vulns/` | Vulnerability summaries per host |
| `GET` | `/api/v1/vulns/scans/` | Recent scan requests / runs (newest first, capped at 100) |
| `POST` | `/api/v1/vulns/scans/{host_id}/` | Queue a Nessus scan for a host (requires TOTP, one active per host) |
| `POST` | `/api/v1/hosts/{id}/approve/` | Approve a pending host (requires TOTP) |
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

## Editions

Vigil Free includes full monitoring, alerting, and unlimited agents, hosts, and
retention. **SQSY Business** adds unlimited sites, the audit log viewer and export,
per-seat pricing with the Operator role, and branding. Business uses a signed
license that is tied to one instance and checked offline. If a Business license
expires, the Business features turn off, and monitoring keeps working. See
[EDITIONS.md](EDITIONS.md).

## License

Vigil's core is licensed under AGPLv3. The Business features are a separate package
under a commercial license. Official images include them. Production use of the
Business features requires a SQSY Business subscription. Customers can request
the Business source code for an audit.
