# Changelog

What changed in each Vigil release, newest first. Versions are `year.minor.patch`: a minor release can be any size, a
patch release fixes things. Business-only items say so; everything else is in every edition.

## 2026.16.0

**Jackil integration** (every edition)
- Settings → Jackil Tickets mints **integration keys** (`vjk_`). Jackil uses one to read hosts, devices, tasks,
  playbooks, runs and automations, server to server, including a host's CPU, memory and open alerts for its link
  previews. A key sees what the admin who made it can see and is stored only as a hash.
- A key changes nothing in Vigil unless an admin ticks **Can change devices** on it. Then, if the admin who made the key
  is still an admin, Jackil can create, edit and delete unmanaged devices: Vigil keeps the only copy of every device,
  and Jackil moves its own device items into Vigil with their links.
- A host's drawer, the Monitor page and a run's details show **Related in Jackil**: the tickets, issues, pages and items
  that link to it.
- Dashboards can hold Jackil's widgets (open tickets, SLA, ticket volume, my issues and more), drawn the same way Jackil
  draws them.
- A new **report chart** widget puts a card from a saved report on a dashboard.
- An alert that clears and fires again notes its Jackil ticket and reopens it, at most once an hour.
- Jackil reads Vigil's signed licence and verifies it itself, so one SQSY Business licence covers both.

**Scoped API keys and webhooks** (Business)
- Settings → API & Webhooks mints **scoped API keys** (`vsk_`) for Vigil's API. A key acts as the admin who made it,
  limited to the sites you pick, and is read-only unless you choose read and write. Keys work on hosts, metrics, alerts,
  tasks, playbooks, software, policies, automations, reports and dashboards; never on accounts, settings, licences,
  keys, stacks or remote sessions. Jackil accepts one in place of an integration key, which is how you limit Jackil to
  some sites.
- **Webhooks** post signed JSON when an alert fires or clears, a task finishes, or a host is approved. Each request
  carries an `X-Vigil-Signature` header (an HMAC-SHA256 of the body with the webhook's secret), and the last 50
  deliveries are kept with their status.

**Devices**
- Unmanaged devices have a **serial**, a **warranty end**, an **owner** (a name or an email) and free-form **extra
  facts** (up to 50, such as Rack: B4), on the Inventory page and in the API.
- Editing or deleting a device is admin-only, as adding one already was. Before, any signed-in user could change or
  delete a device.

**Relays**
- A relay no longer forwards headers that ask a server to treat a request as another method, URL or host
  (`X-HTTP-Method-Override`, `X-Original-URL`, `X-Forwarded-Host` and similar), and never passes a cookie set by Vigil
  back to whoever called the relay.
- An agent's live-session socket path is checked with the same rules as every other agent path.
- The Add Host wizard lists a relay as soon as it is added in Settings, without reloading the page.

**Also**
- The paid edition is called **SQSY Business** everywhere Vigil shows it.
- An automation can allow high-risk steps: a box in its editor, a warning that they will run unattended, and a 2FA code
  to turn it on.
- Edit as YAML opens above the playbook and automation editors.
## 2026.15.1

**Stacks from Git**
- A deploy now rebuilds the stack's images from the new commit (`compose up --build`). Before, compose built an image
  only when none existed, so a new commit kept running the image from the first deploy.
- **Update** on a stack from Git fetches the branch's latest commit, rebuilds and redeploys it. Update on any other
  stack still pulls its images and brings up what changed.
- The host's agent must be 2026.15.1 or newer to rebuild; an older one still deploys, and Vigil says it kept the old
  image.

**Tasks**
- `docker_compose_up` and `stack_deploy` take `build: true`.

**Fixes since 2026.15.0**
- An automation that dispatches to no hosts says why: a high-risk step the automation doesn't allow, a target with no
  hosts, every host in monitor mode, or a playbook that can't be built.

## 2026.15.0

**Relays**
- A relay is a small container agents connect to instead of the dashboard. Run one in an office or on a VPS. The dashboard
  dials out to it, so the dashboard never needs a public address.
- If the dashboard is down, a relay keeps agents' check-ins and hands them over when it's back.
- Agents keep a list of fallback relays and move to the next one when their primary stops answering.
- Turn off the built-in relay and the dashboard refuses any agent that didn't come through a relay.
- Community: one relay (the built-in one or one remote). Business: seats + 2. Hosted relays are coming.

**Web shell, container shell and Web RDP**
- A **Connect** menu on every host: web shell, Web RDP (Windows, Linux with xrdp, and Mac Screen Sharing), the SSH command and the `.rdp` download.
- Container rows get a **Shell** button.
- Off until the host opts in, in its own `agent.yml`. Each session needs a fresh two-factor code or key tap and is logged.
- Business records the whole session.

**Two-factor sign-in for every account**
- A password alone no longer signs anyone in. The next step asks for a passkey or security key tap, or a six-digit code.
- An account with no second factor sets up TOTP on that step before the dashboard opens. Civil SSO sign-ins take the
  same step.
- Lost a phone or key? On the server, `python manage.py reset_2fa <username>` clears that user's factors; they set up
  a new code at their next sign-in.

**Agent profiles and Add Host**
- Add Host shows every agent setting before the install command exists: mode, allowed actions, relays and fallbacks,
  sessions, tags and intervals. The command carries a one-time token, so the agent installs already configured.
- Save those settings as an agent profile (Reimage → Agent profiles). An install profile can name one, so a rebuilt
  machine comes back the same way. Profiles apply at install and rebuild only.
- Settings → Relays: add, order and turn relays on and off, and get install commands that go through a relay.

**Also**
- Power and sleep task actions: sleep and display timeouts, hibernation, power plans, lid actions, sleep and hibernate now.
- Report charts name branches in words ("or if upgrade failed") instead of lane ids.
- Security keys and passkeys (YubiKey, Touch ID, Windows Hello, a phone) answer every confirmation prompt with one tap instead
  of a six-digit code. Add them under Settings → Shared Identity. They need the dashboard on HTTPS.
- macOS agents are Apple Silicon only. Intel Macs are no longer built or offered.

**Licensing**
- Business licenses renew by themselves. A license for a connected install runs about five weeks; there is nothing to
  paste at renewal. Air-gapped installs set `VIGIL_LICENSE_REFRESH=false` and paste a full-term license.
- A license belongs to the install that first refreshes it. Another install using a copy, even of the whole database,
  is refused.
- Release images verify licenses against their built-in key and ignore `VIGIL_LICENSE_PUBLIC_KEY`.
- The About page names who the license belongs to.
- Release images are signed with cosign (wiki: Deployment → Verify the image).

## 2026.14.6

- **Reports**, a page of their own: pick playbooks and automations, then lay out charts on a grid you drag and resize.
  There are seven chart types, an editor with a live preview, and CSV export.
- **Playbooks**: conditions with several rows (all of / any of), or-if branches, and go-to steps that converge
  branches or retry a step.
- New task actions: raise and resolve alerts, send a notification, and open, note and resolve Jackil tickets.
- Editor dialogs resize from a corner grip. Container logs read like a terminal.
- Inventory cells can be selected and copied. The topology map pans, zooms and fits.

## 2026.14.5

- **Topology**: a map of sites, tags, hosts, ports, firewalls, stacks and connections, from what agents report.
- **Playbook inputs**: declare them once, and fill them in from tasks, automations and rollouts.
- Task templates that teach, and template playbooks.
- The Store searches faster, shows sync errors, and can save an app as a task or a policy.

## 2026.14.3

- **The Store**: browse what your machines can install, and install it.
- Containers: images, volumes and networks per host, with remove and prune, plus a host picker and quick filters.
- Stack revisions can be named and pruned. An app can become an update policy.
- The sidebar can show page names.

## 2026.14.2

- **Search everything** from the sidebar or with Ctrl+K.
- Host tags are picked from a list, or created as you type.

## 2026.14.1

- **Git stacks**: create a compose stack from a Git repository; each revision records its commit.
- Rollouts can run several wave groups in parallel.
- The stack editor is a full-screen page.
- Sign-in sessions expire when idle and on a hard cap, with a warning two minutes before.
- One styled two-factor dialog everywhere.
- A security pass: per-host authorization helpers, approved-only agent auth, hashed agent tokens, and signed tasks bound to
  the agent and the time they were sent.

## 2026.14.0

- **Containers**: their own page. Start, stop, restart, read and follow logs, roll back to the previous image, pull from
  private registries. Works with Docker and Podman.
- **Compose stacks** managed by Vigil: an encrypted `.env`, every revision kept, adopt a running stack without
  recreating it.
- **Vulnerabilities, fix first**: a local copy of OSV, CISA KEV and EPSS; findings grouped by the fix that resolves them,
  with one button to deploy that fix; detection tasks; Anvil import.
- **Apps and policies**: an app inventory across every host; present, latest, pinned or absent policies applied in a
  maintenance window, with approvals; patch compliance against an SLA (the report is Business).
- Windows app install, upgrade and uninstall through winget, Chocolatey and the registry.
- New themes: Paper, River and Ink.

## 2026.12

- An audit-driven hardening release:
  - a strict Content Security Policy with no inline scripts;
  - verified agent binaries;
  - monitor-mode agents that run unprivileged;
  - installers that never ship a working credential;
  - a Windows agent that runs as a real service and updates itself safely.
- An uninstaller for every platform.
- Automation triggers with conditions (and, or, real operators). An alert can open a Jackil ticket.
- Change settings in the UI instead of editing `.env` and restarting.
- A dashboard keeps the size you gave it (2026.12.1).

## 2026.11

- **Widget dashboards**: arrange, resize and name cards from a catalog of widgets. Sharing is Business.
- Playbooks, wave groups as tags, and archiving tasks and playbooks.
- Watched processes, GPU readings and Windows update counts.
- Every per-host endpoint is scoped by site.
- A failed playbook stops auto-enrolling to that host (2026.11.3).

## 2026.10

- Fork a baseline or automation from the community catalog with everything it needs.
- The agent no longer leaks PyInstaller's library path into child processes (2026.10.1).

## 2026.9

- **Staged patch rollouts**: waves with a validation window and a halt-on-failure gate. Automations can dispatch wave by
  wave.
- Remediate-by dates from an editable policy and the CISA KEV catalog; host scores rise as findings go overdue.
- Windows Update actions, and reboots a logged-in user can defer.
- Tags become real records, with a tag picker on every tag field.
- Share baselines and automations through the community repo (2026.9.1).

## 2026.8

- `add_tag` and `remove_tag` task actions.
- The latest Django 5.2 LTS, logo images, and automation text and host filters.

## 2026.7

- **Firewall management**: read and edit ufw, firewalld and Windows Firewall. Vigil refuses any change that would lock you
  out.
- **Reprovisioning UI**: an image library you can pull into or upload an ISO to, and a rebuild ceremony.
- Trivy findings are deduplicated and wired to Suggest Fix.

## 2026.6

- **Reprovisioning**: rebuild a host unattended from Ubuntu, Debian or RHEL images, behind an explicit opt-in on the agent
  and a three-factor ceremony.
- Alerts are suppressed for hosts in a maintenance window.

## 2026.5

- **Per-site roles** and a customisable Operator (Business), with every list and object scoped to the sites you can see.
- Branding (Business), run history for baselines and automations, and a layout density toggle.
- Remote access over Cloudflare Tunnel, Tailscale or a reverse proxy, set with one variable.

## 2026.4

- **Licensing**: two tiers, Community and Business, verified offline. Sites, the audit log and Operator roles are
  Business.
- Civil sign-in (opt-in), shared across SQSY apps.
- Automations, per-use task inputs, and public status pages with uptime history.

## 2026.3

- Docker container inventory and image-update checks that don't use pull quota, with a one-click fix that recreates the
  container on the new image.
- Timed and permanent alert acknowledgements, with bulk actions.
- An inventory for network gear that doesn't run an agent, and an SSH command button.
- TimescaleDB storage policies, and a new eye mark.

## 2026.2

- Pluggable vulnerability scanners: Trivy and Greenbone.
- Conditional task steps (`when:`) and `target_tags`.
- Built-in task templates and an About page.
