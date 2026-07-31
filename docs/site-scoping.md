# Sites: one global scope

Status: approved design · 2026-07-29 · Owner: Connor

This document covers the site *scope model* and the first UI that depends on
it. It supersedes the two-row Default/Global arrangement introduced on
`feature/site-scoping-foundations`.

---

## 1. The decision

A site is an administrative boundary — a campus, a department, a client org.
Free installs have exactly one; a Business license lifts the limit.

Until now there were **two** structural rows with **two** different fallback
rules:

| Row | Flag | What fell back to it | Shipped in |
|---|---|---|---|
| Default | `is_default` | unassigned **hosts** | v2026.4.0 |
| Global | `is_global` | unassigned **policies** | unmerged branch |

Two names for "the place things go when you haven't said otherwise" is one
concept too many. It shows up as two undeletable rows in the Sites pane, two
special cases in the tile grid, two recovery paths when a site is deleted,
and — worst — an unanswerable RBAC question: does a role on Global grant the
hosts sitting in Default?

**Decision: collapse to one row.** A single `Site` carrying `is_global=True`
holds unassigned hosts *and* is the scope whose baselines, automations, and
notification channels cascade into every other site.

### Where the model lives

`Site` stays in `apps_business/sites/` (commercial). Core reads it through
the façade at `server/vigil/scoping.py`.

The alternative — moving `Site` into AGPL core so permission code could join
to it like any other relation — was considered and rejected *for now*. The
argument for moving it was that core querysets can reach site data via the
reverse accessor `site_assignment` without any import, which works because
Django registers `related_name` on the *target* model:

```python
Host.objects.filter(site_assignment__site=site)   # no import required
```

…and therefore that the rule "core routes site queries through the façade"
is unenforceable, since a direct traversal is indistinguishable from a
façade call in review and passes CI identically.

That risk is real but does not apply to how Vigil ships. `apps_business` is
in `INSTALLED_APPS` unconditionally ("installed always, unlocked by
license"); no build strips it. The failure mode below only reaches a third
party exercising their AGPL right to delete the commercial directory:

```
apps_business installed: NONE
'site_assignment' registered on Host: False
FieldError: Cannot resolve keyword 'site_assignment' into field.
```

Note it is raised when the queryset is **built**, not when the module is
imported — the process boots clean and fails on that one line. §4 turns this
into a failing build instead of a latent trap.

Moving `Site` to core remains available later and does not get meaningfully
harder for waiting.

---

## 2. Data model

### 2.1 Changes

- **Remove** `Site.is_default`. One flag, `is_global`, carries both meanings.
- **Exactly one** row has `is_global=True`. It cannot be deleted — `ValueError`
  at the model layer, HTTP 400 at the API layer.
- Unassigned hosts belong to it. Unassigned baselines, automations, and
  notification channels resolve to it.
- It is named **Global**, slug `global`.

Everything else from `feature/site-scoping-foundations` stands unchanged:
`BaselineSiteAssignment`, `AutomationSiteAssignment`, `ChannelSiteAssignment`,
`GlobalSuppression`, and `UserSiteRole` keep FKing into core, and core gains
no columns.

### 2.2 Migration

Add `apps_business/sites/migrations/0005_unify_global_site.py`:

1. Delete the row with `slug="global"` created by `0003_global_site` (it
   exists only where that unmerged branch has been run; a real v2026.4.0
   install has never seen it).
2. On the surviving `is_default=True` row, set `is_global=True`, `name="Global"`,
   `slug="global"`. Host assignments FK by id, so renaming moves nothing.
3. `RemoveField` `is_default`.

Steps 1 and 2 must run in that order: `Site.slug` and `Site.name` are both
`unique`, so renaming before deleting raises `IntegrityError` on any install
that ran the foundations branch.

Reverse: recreate `is_default`, set it on the global row, restore
`name="Default"`/`slug="default"`, clear `is_global`.

An additive fix-up migration is used rather than revising `0003`, so the
pushed foundations branch needs no force-push. If that branch is instead
revised before merge, fold these operations into `0003` and delete this one —
the end state is identical.

### 2.3 Call sites to update

`Site.is_default` is referenced in exactly seven places. (`AlertRule.is_default`
is an unrelated model and must not be touched.)

| File | Change |
|---|---|
| `apps_business/sites/models.py` | drop the field; delete guard keys on `is_global` only |
| `apps_business/sites/views.py:47` | fold unassigned host count into the global row |
| `apps_business/sites/views.py:78` | one delete guard, not two |
| `apps_business/sites/serializers.py` | drop `is_default` from `fields` and the "cannot remove default flag" validation |
| `apps_business/sites/admin.py` | `list_display` shows `is_global` |
| `static/js/vigil-sites.js:47,51,125` | hide Delete and show the badge for `is_global`; badge text becomes `global` |
| `apps_business/sites/tests.py` | update the six assertions that name `is_default` |

---

## 3. Core reads site

### 3.1 Façade addition

```python
def sites_for_hosts(host_ids) -> dict:
    """Map host id -> Site for the given ids. Unassigned hosts map to the
    global site. Returns {} when the Business sites app is absent."""
```

One query for the assignments plus one for the global site — not N. This is
the only new façade function; `resources_for`, `scope_of`, `suppress`,
`unsuppress`, and `execution_allowed` are unchanged.

### 3.2 Host payload

`GET /api/v1/hosts/` gains two keys per host, following the flat
id-plus-label style already used by the automations API (`task_definition` /
`task_name`):

```json
{ "hostname": "atlas", "site": "<uuid>", "site_name": "Global" }
```

Both are `null`/`""` when the Business app is absent. Every host has a site,
because unassigned resolves to Global.

This **replaces** the previously proposed `GET /api/v1/sites/assignments/`
endpoint. With site on the host payload, the assign modal needs one existing
call instead of two, and no new endpoint exists to keep in sync.

---

## 4. The AGPL-only guard

Add `server/vigil/settings_agpl_only.py`:

```python
from .settings import *  # noqa
INSTALLED_APPS = [a for a in INSTALLED_APPS if not a.startswith("apps_business")]
```

Add one test that runs a subprocess under those settings and asserts exit 0.
Removing an app from `INSTALLED_APPS` with `override_settings` does **not**
unregister reverse relations, so this must be a real subprocess.

The subprocess exercises every core path that reads site data: `scope_of`,
`resources_for`, `execution_allowed`, `sites_for_hosts`, and the queryset the
hosts list view builds. Any of them reaching a Business relation by name
fails the build with a `FieldError` instead of rotting silently.

This is the enforcement that §1 traded away by leaving `Site` in Business. It
is the whole reason option B is acceptable.

---

## 5. Tab accents

`.sub-tabs` appears in one template (`_baselines.html`, two tabs) and
`.sub-tab.active` hardcodes `border-bottom-color: var(--mint)`, so both tabs
underline the same colour.

Add `t-*` accent modifiers to `.sub-tab`, mirroring the five that `.tab`
already has:

```css
.sub-tab.t-rose.active  { color: var(--rose);     border-bottom-color: var(--rose); }
.sub-tab.t-lemon.active { color: var(--lemon);    border-bottom-color: var(--lemon); }
.sub-tab.t-mint.active  { color: var(--mint);     border-bottom-color: var(--mint); }
.sub-tab.t-lav.active   { color: var(--lavender); border-bottom-color: var(--lavender); }
.sub-tab.t-sky.active   { color: var(--sky);      border-bottom-color: var(--sky); }
```

`.sub-tab` sets the underline with `border-bottom-color`, not the `::after`
+ `currentColor` trick `.tab` uses, so each rule sets both properties.

Assignments follow the precedent already set on the Tasks page, where
"My Library" is `t-lav` and "History" is `t-mint`:

- **Baselines → `t-lav`** — a library of defined sequences, the same role as
  Tasks' "My Library".
- **Automation → `t-sky`** — Sky is the design language's interactive accent,
  and automations are the triggered thing.

This deliberately leaves `t-mint` free to mean "History" on both panels,
which the run-history spec will use.

The heavier `.sub-tab` metrics (14px/600, 18px padding) stay as they are:
sub-level navigation should not read as top-level.

---

## 6. Assign-hosts modal

Today the modal renders every host as a flat checkbox of hostname only,
nothing pre-checked, with no indication of current membership — while telling
the user "a host belongs to exactly one site."

It becomes a membership editor, following the deploy modal's established
idiom (search input with an `oninput` filter, table with a select-all head
row, `.host-pick-empty` empty state, summary hint line) rather than inventing
a new one.

### 6.1 Behaviour

- **Search** filters by hostname on every keystroke.
- **Status chips** — online / offline / pending.
- **Tag chips** derived from the tags actually present on the loaded hosts,
  not free-form entry.
- Search, status, and tags compose as one predicate over a single in-memory
  host list. No server round-trip while filtering.
- Hosts already in this site render **pre-checked**, with their current site
  shown inline.
- **Unchecking removes**: `DELETE /api/v1/sites/{site}/hosts/{host}/`. A host
  removed from a site is unassigned, which *is* membership of Global — so the
  row's site label becomes "Global" rather than blank.
- Opening the modal **on the Global site** is therefore the inverse: checking
  a host there means "no specific site", and saves as a `DELETE` of that
  host's assignment. There is no `PUT` to Global, because unassigned and
  assigned-to-Global must not be two different states in the database.
- The primary button reads **Save changes**, not "Assign selected" — it no
  longer only assigns.

### 6.2 Save semantics

Save computes a diff against the membership captured when the modal opened:

- checked and not previously in this site → `PUT`
- unchecked and previously in this site → `DELETE`
- everything else → no request

Requests are issued for the diff only. On partial failure the modal stays
open, reports how many of each operation succeeded, and reloads the site list
so the UI reflects what actually happened rather than what was attempted.
This matches the current handler, which leaves the modal open and writes the
error into `#sa-note`.

### 6.3 Escaping

All host-derived strings — hostname, tags, site name — render through
`escHtml` or `textContent`, never raw interpolation. Tags are user-supplied.

---

## 7. Testing

| Area | Test |
|---|---|
| Migration | one global row after migrate; no row has `is_default`; a pre-existing default row with host assignments keeps them |
| Delete guard | model raises `ValueError`; API answers 400; row survives |
| Host payload | every host reports a site; unassigned reports Global; assigned reports its own |
| `sites_for_hosts` | maps assigned and unassigned correctly; constant query count regardless of host count |
| AGPL-only guard | subprocess under `settings_agpl_only` exits 0 |
| Serializer | `is_global` is read-only; POST with `is_global: true` does not create a second global row |

Frontend behaviour (chips, diff-save) is not covered by the Django suite,
which executes no JS. It is deferred to the Playwright pass already
outstanding from M3, and noted there rather than silently skipped.

Baseline before starting: **278 tests pass.**

---

## 8. Out of scope

Deferred to their own specs, in this order:

1. **Run history** for baselines and automations. `TaskRun` already models the
   grouping, but `apps/automations/engine.py` creates tasks with no `run`, so
   an execution leaves only orphan `Task` rows identified by a `step_label`
   string. That is why there is nothing to show today.
2. **Per-site role resolution.** `UserSiteRole` exists as a table; nothing
   reads it. Includes the rule that a user granted the global site holds it
   as their only site.
3. **Customisable Operator capabilities** — permissions per site, per app,
   per capability. Depends on 2.

Also out of scope: the site tile grid on the home screen, sticky site
context, and moving `Site` into AGPL core.
