# Extension contract

Vigil is one repo, two tiers (see `docs/EDITIONS.md`). Business features ship
IN this repo under `server/apps_business/` (commercial license, runtime
`has_feature()` gates) — the separate Vigil-Pro / Vigil-Enterprise repos were
retired in 2026.4.0 before ever shipping.

The seams below remain the contract for **community/operator extensions**
(`VIGIL_EXTRA_APPS`): extra Django apps that subscribe to lifecycle events,
register capabilities, and mount routes without forking core. Business apps
use the same hooks bus but are wired explicitly in `INSTALLED_APPS`.

## 1. App loading — `VIGIL_EXTRA_APPS`

Edition apps are placed on the `PYTHONPATH` and named in a comma-separated env
var. `settings.py` appends them to `INSTALLED_APPS`:

```bash
VIGIL_EXTRA_APPS=vigil_pro.rbac,vigil_pro.baselines
```

Each entry is a normal Django app. It may have its own models, migrations,
admin, and templates. Because it's a real app, its migrations run on
`migrate` like any other.

## 2. Lifecycle events — `vigil/hooks.py`

Core emits a small, documented set of events; editions subscribe in their
`AppConfig.ready()`. Core never knows who (if anyone) is listening.

| Event | Payload | Emitted when |
|---|---|---|
| `host_approved` | `host`, `approved_by` | a pending host is approved |
| `host_rejected` | `host`, `rejected_by` | a pending host is rejected |
| `insight_created` | `insight` | *(reserved — not yet emitted)* |
| `alert_fired` | `alert` | *(reserved — not yet emitted)* |
| `task_completed` | `task` | *(reserved — not yet emitted)* |

```python
# vigil_pro/baselines/apps.py
from vigil import hooks

class BaselinesConfig(AppConfig):
    def ready(self):
        hooks.subscribe("host_approved", self.dispatch_baselines)
```

Handlers are isolated — a raising handler is logged and swallowed. Adding a new
event name to `KNOWN_EVENTS` + an `emit()` call is a core change; **removing or
renaming one is a breaking change** to the contract.

## 3. Feature gating — `vigil/editions.py`

Editions advertise capabilities; core lights up integration points without
importing edition code.

```python
# edition app, at startup:
from vigil import editions
editions.register_feature("ai_suggestions")

# core, anywhere:
if editions.feature_enabled("ai_suggestions"):
    ...  # show "Suggested fix" button
```

`active_edition()` returns `community` / `pro` / `enterprise` for display
(About page exposes `edition` + `features`). This is a UX/wiring switch, **not
license enforcement** — real licensing lives in the future Enterprise SaaS layer.

Feature → edition mapping is the catalog in `FEATURE_EDITIONS`; keep it in sync
with `docs/EDITIONS.md`.

## 4. URL mounting

Any extra app exposing a `urls.py` is auto-mounted by `vigil/urls.py` at
`ext/<app-label>/`. Apps without one are skipped. The reference app serves
`GET /ext/example_extension/ping/`.

---

## Data model rule: edition tables reference core, never the reverse

Core has **no** reserved columns for Pro/Enterprise. An edition that needs to
attach data to a core object adds its **own** model with a FK into core:

```python
# vigil_enterprise/sites/models.py
class HostSiteAssignment(models.Model):
    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE)
    site = models.ForeignKey("Site", on_delete=models.CASCADE)
```

This keeps all edition schema in edition migrations, so core's schema stays
identical across all three editions and uninstalling an edition never orphans a
core column.

---

## Checklist before changing core

- [ ] Did I keep every `KNOWN_EVENTS` name and payload stable?
- [ ] Did I avoid importing or hard-coding any edition module?
- [ ] If I added an integration point, is it gated by `feature_enabled(...)`?
- [ ] Do the seam tests (`apps/example_extension/tests.py`) still pass?
