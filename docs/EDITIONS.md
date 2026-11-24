# Free and Business editions

Vigil comes in two editions: Free and Business.

- **Free** is for monitoring your own machines. It covers monitoring, alerts,
  remote tasks, vulnerability scanning, and patch policies, with no limit on
  agents, hosts, or retention.
- **Business** adds the features you need to prove to someone else that their
  systems are running correctly. Examples are the compliance report, the audit
  log viewer, multiple sites, extra roles, scoped API keys, and webhooks.

Business is licensed per seat. Free has no feature or host limits that stop
monitoring.

## Feature matrix

| Feature | Free | Business |
|---|:--:|:--:|
| Monitoring and alerting with default rules, unlimited agents, hosts, and retention | Yes | Yes |
| Remote tasks and the agent (the agent is open source and does not check the edition) | Yes | Yes |
| Vulnerability scanning with Trivy, Nessus, or Greenbone | Yes | Yes |
| Vigil's own vulnerability matcher (OSV, KEV, and EPSS data, offline bundle), with evidence and fix groups | Yes | Yes |
| Remediation by fix (deploy a fix to every affected host), and AI mitigations when no fix exists | Yes | Yes |
| Detection tasks (severity, CVEs, boost), Organization, Community, and Vendor tags, Anvil SARIF import | Yes | Yes |
| Containers and stacks (Docker and Podman): lifecycle, logs, rollback, single-host stack editor and `.env` file, adopt | Yes | Yes |
| Audit trail of stack and `.env` events (viewer) | No | Yes |
| **Baselines** (run automatically when a host is approved) | Yes | Yes |
| **AI suggestions**: use your own endpoint, any OpenAI-compatible service or Anthropic | Yes | Yes |
| **Status page** (public token URL, with a "Powered by Vigil" badge) | Yes | Yes |
| Jackil integration (alert to ticket) | Yes | Yes |
| **App and patch policies** (present, latest, pinned, or absent; patching windows; deferral; approve each change) | Yes | Yes |
| Fleet view by update, and fleet-wide approve or decline | Yes | Yes |
| Patch compliance figures (percentage of hosts patched within the SLA) | Yes | Yes |
| **Compliance report**: pass or fail per site, CSV export, printable branded report with "Licensed to" | No | Yes |
| Seats | 1 admin and 1 viewer | Per seat |
| Roles | Admin, Viewer | Adds Operator and custom roles |
| **Sites** (administrative boundaries, with per-site scoping) | 1 (default) | Unlimited |
| **Audit log** viewer and CSV export (recording is always on) | No | Yes |
| **Status page branding** (logo, no badge, per-site client pages) | No | Yes |
| **Branding** (reports and portal) | No | Yes |
| **Scoped API keys** (bearer keys for the API, limited to sites, read-only or read-write) | No | Yes |
| **Webhooks** (signed JSON for alert, task, and host events) | No | Yes |
| SSO and SAML | No | Contact us |

Seats are the only thing Business charges for. Sites are unlimited. Hosts and
agents are never priced.

### What a site is

A site is an administrative boundary, such as a campus, a department, or a
client organization. It is not a physical location. You can put any hosts you
want into one site.

If you manage four houses, you can put all of them in one site. Create more
sites only when you need separate administration for each group.

There is no Pro edition and no Enterprise edition. Vigil has two editions only.

## How the license works

- Vigil is a single image, `susquehannasyntax/vigil:<version>`. The core is
  open source under AGPLv3. The Business features are a separate package with
  a commercial license. Official images include them. You can request the
  Business source code for an audit.
- A build from the public repository contains the AGPL core only, without the
  Business features.
- A Business license is a signed file. Vigil checks the signature on the
  machine, using a public key built into the release image. Vigil does not
  need an internet connection to check the license, and it has no kill switch.
- A license for a connected install lasts about five weeks. Vigil renews it
  automatically. A copied license stops working within about a month. Only one
  install can hold a license at a time. A copied database does not get
  around this.
- Air-gapped installs set `VIGIL_LICENSE_REFRESH=false`. They receive a
  full-term license that you paste in by hand.
- Each license has an ID and the buyer's organization name. You can see both
  under **Settings > License** and on the About page.
- Release images are signed with cosign. You can check that the image you
  pulled is the one we built.
- A license works only on the instance UUID it was issued for. You can find
  the UUID on your license screen.
- Vigil never stops monitoring. If the license expires, Business features stay
  on for a 14-day grace period, then turn off. Banners tell you what changed.
  If you have more seats in use than your license allows, Vigil keeps working
  and shows a banner. If a license is for a different instance, Vigil uses the
  Free edition and shows a banner. Monitoring, alerting, and agent check-ins
  keep working in all of these cases.

## Enter a license

There are three ways to enter a license. Vigil applies it without a restart.

1. Set the `VIGIL_LICENSE_KEY` environment variable. This value takes priority
   over the other two methods.
2. Run `manage.py license set <blob>`.
3. Paste the license into the box on **Settings > License**.

The license screen shows the edition, the organization, the instance UUID, the
expiry date, seats used and allowed, and what each Business feature does.

To buy a license, go to susquehannasyntax.com/subscribe with your instance UUID.
The license is sent to you by email and shown on screen.

## AI uses your own key

Vigil does not pay for AI model usage, and Business does not include a hosted
model. Both editions use the same setup. You point Vigil at an
OpenAI-compatible endpoint, such as Ollama, vLLM, LM Studio, or OpenAI, or at
Anthropic. You provide your own key and choose the model.

Vigil treats AI output as untrusted. It checks every response before showing
it. The AI cannot change an agent's configuration, and Vigil does not run any
action that an AI suggests without you approving it.

## Telemetry

Only a Business install with a license contacts the license server. It contacts
the server only to renew the license. It checks at most every six hours and
usually about once a week.

The request contains:

- Your instance UUID
- The Vigil version
- The number of seats in use
- The install's public key
- A signature over those values

Nothing else is sent. Air-gapped installs set `VIGIL_LICENSE_REFRESH=false` and
paste a full-term license instead. Free installs send nothing.
