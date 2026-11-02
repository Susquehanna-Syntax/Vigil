# Vigil tiers

There are exactly two, and there will not be a third.

> **Free Vigil answers "is my stuff up." Business Vigil answers "can I prove
> to a third party that their stuff is up."**

A homelabber never crosses that line. An MSP or an internal IT department
crosses it on day one. That's the whole pricing model — gates sit on the
*accountability* axis only. Never on scale: **no agent caps, no host caps,
no retention caps, ever.** Anything that merely makes Vigil more useful is
free, because useful-but-not-needed is simultaneously the most pirateable
and least buyable category there is.

There is no Pro tier (2026.4.0 folded the never-shipped Pro feature set into
Free) and no Enterprise tier (internal test licenses are Business licenses
with `seats: 9999`). SSO/SAML is "available — talk to us," not a tier.

## Feature matrix

| Feature | Free | Business |
|---|:--:|:--:|
| Monitoring, alerting (smart defaults), unlimited agents/hosts/retention | Yes | Yes |
| Remote task execution + agent (agent is 100% OSS, no tier awareness) | Yes | Yes |
| Vuln scanning (Trivy / Nessus / Greenbone) | Yes | Yes |
| Vigil's own vulnerability matcher (OSV / KEV / EPSS, offline bundle), evidence and fix groups | Yes | Yes |
| Remediation by fix (deploy a fix to every affected host), AI mitigations when there is no fix | Yes | Yes |
| Detection tasks (severity / cves / boost), Organization · Community · Vendor tags, Anvil SARIF import | Yes | Yes |
| Containers & stacks (Docker / Podman): lifecycle, logs, rollback, single-host stack editor and .env, adopt | Yes | Yes |
| Audit trail of stack and .env events (viewer) | — | Yes |
| **Baselines** (auto-dispatch on host approval) | Yes | Yes |
| **AI suggestions** — BYO endpoint, any OpenAI-compatible or Anthropic | Yes | Yes |
| **Status page** (public token URL, Powered-by-Vigil badge) | Yes | Yes |
| Jackil integration (alert → ticket) | Yes | Yes |
| **App & patch policies** (present / latest / pinned / absent, patching windows, deferral, approve each change) | Yes | Yes |
| Fleet view by update, fleet-wide approve / decline | Yes | Yes |
| Patch compliance numbers (% of hosts patched within SLA) | Yes | Yes |
| **Compliance report** — pass/fail per site, CSV export, branded printable report with "Licensed to" | — | Yes |
| Seats | 1 admin + 1 viewer | **per-seat pricing** |
| Roles | Admin, Viewer | + **Operator**, custom roles |
| **Sites** (administrative boundaries; per-site scoping) | 1 (default) | **unlimited** |
| **Audit log** viewer + CSV export (recording is always on) | — | Yes |
| **Status page branding** (logo, no badge, per-site client pages) | — | Yes |
| **Branding** (reports, portal) | — | Yes |
| **Scoped API keys** (bearer keys on the general API, limited to sites, read-only or read-write) | — | Yes |
| **Webhooks** (signed JSON on alert, task and host events) | — | Yes |
| SSO/SAML | — | talk to us |

**Seats are the only meter.** Sites are unlimited on Business because seats
and sites anti-correlate across real customers (solo MSP: 1 seat, 40 sites;
internal IT: 30 seats, 4 sites) — a seat×site grid punishes one of them.
Never per-host pricing — that punishes the 1–50 device sweet spot that is
the whole pitch.

### What a "site" is (this definition is load-bearing)

> A site is an **administrative boundary** — a campus, a department, a
> client org. It is not a physical location. Put whatever you want inside
> one.

A homelabber with four houses consolidates into one site and never hits a
wall. An org that genuinely needs separate sites needs them for reasons no
config trick solves — org charts don't consolidate.

## How the gate works (and doesn't)

- One image (`susquehannasyntax/vigil:<version>`). The core is AGPLv3 and
  public; the Business apps are a separate package (`vigil-business`, a
  private repo, commercial license) that official images install compiled.
  Its source is available to customers for audit on request. All migrations
  run on every install; features unlock at runtime. A build from the public
  repo is the AGPL core, without the Business apps.
- A **Business license** is an Ed25519-signed blob verified locally against
  a public key built into the release image (`VIGIL_LICENSE_PUBLIC_KEY` is
  for source checkouts only, so nobody can point Vigil at a key of their
  own). Offline verification. No CRL, no kill switch — air-gapped deploys
  are first-class.
- A license for a connected install runs about **five weeks** and Vigil
  **renews it on its own**, so a copied key stops working within a month.
  Each refresh is signed with a key this install keeps apart from its
  instance UUID; the first install to refresh holds the license and any
  other install is refused (a copied database included). Air-gapped
  installs set `VIGIL_LICENSE_REFRESH=false` and get a full-term license
  they paste by hand.
- Every license carries a random **license id** and the buyer's org, shown
  on Settings → License and the About page, so a key posted publicly traces
  back to who bought it.
- Release images are **signed with cosign**, so you can check what you
  pulled is what we built.
- Licenses are **instance-bound**: your license carries the instance UUID
  shown on your license screen and does nothing anywhere else. That closes
  casual key-sharing (a key on Reddit licenses nobody). Someone determined
  enough to patch the source can — that person was never a customer, and no
  amount of hardware fingerprinting changes it, so we don't fingerprint.
- **Nothing ever blocks.** Expiry → 14-day grace with Business features on →
  Business features off, banners throughout. Seat overage → works, banner.
  Wrong instance → free tier, banner. Monitoring, alerting, and agent
  ingest are untouched by every one of those states, forever. A licensing
  code path that could degrade monitoring is a severity-one bug.

## License input paths

1. `VIGIL_LICENSE_KEY` env var (GitOps-friendly; wins over everything)
2. `manage.py license set <blob>`
3. Paste box on the Settings → License screen

Applied at runtime — no restart. The license screen shows tier, org,
instance UUID, expiry, seats used/allowed, and what each locked feature
does. Buying happens at susquehannasyntax.com/subscribe with your instance
UUID; the blob arrives by email *and* on-screen.

## AI is bring-your-own-key and provider-agnostic

Vigil **never pays per-token inference**, and Business doesn't get a hosted
model either — the same BYO code path serves both tiers. The operator points
Vigil at any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, OpenAI) or
Anthropic, brings their own key, and picks the model. LLM output is
untrusted and always passes through `parse_and_validate`; `update_agent` is
never accepted from a model; nothing is auto-executed.

## Telemetry, in full

Only an install with a Business license calls out, and only to renew it
(`GET /license` on the license server, at most every six hours and usually
about once a week). It sends: your instance UUID, Vigil version, seats in
use, this install's public key, and a signature over those. That is the
complete list. Air-gapped installs set `VIGIL_LICENSE_REFRESH=false` and
re-paste a full-term license. Free installs send nothing, ever.
