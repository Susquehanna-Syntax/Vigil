# Vigil editions

Vigil is **open-core**. The Community edition is free and complete for
homelabs; Pro and Enterprise are commercial editions built in separate repos
that plug into core (see `docs/pro-extension-points.md`).

All editions are **self-hosted today.** Enterprise gains a managed **SaaS**
option in the future; everything below ships self-hosted first.

## Feature matrix

| Feature | Community | Pro | Enterprise |
|---|:--:|:--:|:--:|
| Monitoring, alerting (smart defaults) | ✅ | ✅ | ✅ |
| Remote task execution + agent | ✅ | ✅ | ✅ |
| Vuln scanning (Trivy / Nessus / Greenbone) | ✅ | ✅ | ✅ |
| Single admin user | ✅ | — | — |
| Multi-user **RBAC** (ADMIN / OPERATOR / VIEWER) | — | ✅ | ✅ |
| **Baselines** (auto-dispatch on host approval) | — | ✅ | ✅ |
| **AI task suggestions** (BYO key, any provider) | — | ✅ | ✅ |
| **Status pages** | — | ✅ | ✅ |
| **Detailed audit logs** + retention | — | — | ✅ |
| **Sites** / multi-tenancy (per-site operator scoping) | — | — | ✅ |
| **Branding**, **SSO**, **federation** | — | — | ✅ |
| Email / SLA support | — | — | ✅ |
| Managed SaaS hosting | — | — | 🔜 future |

Repos: Community = this repo · Pro = `Vigil-Pro` · Enterprise = `Vigil-Enterprise`.

## Pricing (direction)

Never per-host — that punishes the 1–50 device sweet spot that is the whole
pitch. Price on users / sites / audit / support / SSO.

| Tier | Audience | Target price |
|---|---|---|
| **Community** | Homelabbers | Free (AGPLv3) |
| **Pro** | Extreme homelabbers / solo businesses | ~$8/mo · $79/yr per instance |
| **Enterprise** | SMB / MSP | ~$199–349/mo per instance (custom above 50 hosts / SaaS) |

Annual ≈ 2 months free. A launch-only one-time "lifetime homelab" license
(~$99) is worth considering for goodwill, not as a permanent SKU.

## AI is bring-your-own-key and provider-agnostic

Vigil **never pays per-token inference.** AI task suggestions require the
operator to supply their own API key for a provider of their choice
(Anthropic / OpenAI / OpenAI-compatible-local such as Ollama). Vigil ships
pluggable provider adapters; the operator brings the key and picks the model.
Anthropic is the documented default (prompt caching on the action registry
makes it cheap for the operator), but never required. LLM output is untrusted
and always passes through `parse_and_validate` before use.
