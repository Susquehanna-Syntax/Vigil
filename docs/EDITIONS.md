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
| Monitoring, alerting (smart defaults), unlimited agents/hosts/retention | ✅ | ✅ |
| Remote task execution + agent (agent is 100% OSS, no tier awareness) | ✅ | ✅ |
| Vuln scanning (Trivy / Nessus / Greenbone) | ✅ | ✅ |
| **Baselines** (auto-dispatch on host approval) | ✅ | ✅ |
| **AI suggestions** — BYO endpoint, any OpenAI-compatible or Anthropic | ✅ | ✅ |
| **Status page** (public token URL, Powered-by-Vigil badge) | ✅ | ✅ |
| Jackil integration (alert → ticket) | ✅ | ✅ |
| Seats | 1 admin + 1 viewer | **per-seat pricing** |
| Roles | Admin, Viewer | + **Operator**, custom roles |
| **Sites** (administrative boundaries; per-site scoping) | 1 (default) | **unlimited** |
| **Audit log** viewer + CSV export (recording is always on) | — | ✅ |
| **Status page branding** (logo, no badge, per-site client pages) | — | ✅ |
| **Branding** (reports, portal) | — | ✅ |
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

- One repo, one image (`sqsy/vigil:<version>`). Business code lives in
  `server/apps_business/` under a commercial license
  (`server/apps_business/LICENSE`); everything else is AGPLv3. All
  migrations run on every install; features unlock at runtime.
- A **Business license** is an Ed25519-signed blob verified locally against
  a public key baked into the build. Offline. No phone-home, no CRL, no
  kill switch — air-gapped deploys are first-class.
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

The optional monthly license refresh (`GET /license?instance=`) sends: your
instance UUID, Vigil version, and seat count. That is the complete list.
Air-gapped installs turn refresh off and re-paste annually. Free installs
send nothing, ever.
