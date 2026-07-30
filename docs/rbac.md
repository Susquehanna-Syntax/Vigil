# RBAC: per-site roles and customisable Operator

Status: approved shape, design for review · 2026-07-29 · Owner: Connor

Covers what were originally two specs — per-site role resolution and the
customisable Operator. They are one document because the agreed shape makes
them inseparable: once Admin and Viewer are themselves per-site, "which role
applies here" and "what may that role do here" are two halves of one
resolution function.

Depends on `docs/site-scoping.md` (one global scope) being merged.

---

## 1. The resolution rule

Every authorisation question becomes `can(user, site, app, verb)`. It
resolves in two stages: find the role, then ask what the role permits.

### 1.1 Finding the role

```
role_of(user, site):
    1. user.is_superuser            -> OWNER      (global, absolute)
    2. rows = UserSiteRole[user]
    3. if not rows                  -> today's answer, unchanged:
                                         ADMIN if user.is_staff
                                         else profile.role or VIEWER
    4. if rows[site]                -> that role
    5. if rows[global site]         -> that role            (the floor)
    6. otherwise                    -> NONE
```

Step 3 reproduces the current `role_of()` **exactly**, including its
`is_staff` short-circuit. The site-scoped branches only engage once a user
has at least one row.

Three consequences worth stating plainly:

**Step 3 keeps every existing install working.** Nobody has `UserSiteRole`
rows today, so everyone keeps exactly the access they have now and the
migration is a no-op. Per-site roles are opt-in.

**Step 6 is the sharp edge.** A user with *some* rows and no global row gets
nothing on sites they hold no row for. So granting someone their first
per-site role **narrows** their access — from "everywhere via profile" to
"only here". That is the correct least-privilege behaviour, but it is
surprising, so the grant UI must say it at the moment of granting, not in
documentation:

> Dana currently has Operator access to every site through their profile.
> Granting a site role will limit them to only the sites listed here.

**Step 5 is the floor.** A global row applies to every site that has no more
specific row, including sites created later. `Global: viewer` +
`West Campus: operator` means operator at West Campus and viewer everywhere
else.

### 1.2 Owner, and not locking yourself out

`OWNER` maps onto the existing `is_superuser` field. No new column, and the
escape hatch already exists on every install — superusers are ADMIN today, so
promoting them to OWNER takes nothing away from anyone.

**`is_staff` keeps its meaning.** An earlier draft of this design had
`is_staff` stop implying ADMIN, on the reasoning that scopeable Admin plus a
global staff short-circuit leaves the matrix decorative. That was wrong on
two counts. It would demote every existing staff user on upgrade — a silent
lockout is a worse failure than a too-broad grant — and 28 of the current
tests create their admin with `is_staff=True`, so the suite would have gone
red for a reason unrelated to what was being tested.

The short-circuit is confined to step 3 instead. A staff user with no rows
behaves exactly as today; a staff user who is *given* rows becomes scoped
like anyone else. The matrix is opt-in, consistent with every other part of
this design, and **no migration is required at all**.

Two guards, both enforced server-side:

- The last remaining superuser cannot be demoted or deleted.
- A user cannot remove their own OWNER flag.

---

## 2. What is site-scoped, and what is not

Per your constraint: *tasks are universal, so are accounts and settings.*

| Area | Scoped? | Why |
|---|---|---|
| Hosts | **yes** | a host belongs to exactly one site |
| Baselines | **yes** | already scoped by `BaselineSiteAssignment` |
| Automations | **yes** | already scoped by `AutomationSiteAssignment` |
| Alerts | **yes** | an alert belongs to its host's site |
| Status pages | **yes** | published per site |
| Task **library** | no | definitions are shared authoring artifacts |
| Task **execution** | yes | running a task targets hosts, which are scoped |
| Accounts | no | one user list per instance |
| Settings | no | instance-wide configuration |
| Licensing | no | instance-wide |

The task split is the subtle one: *writing* a task definition is universal,
*running* it is scoped, because a run targets hosts and hosts have sites.
`tasks:run` is therefore checked against the target host's site, while
`tasks:edit` is checked once, globally.

---

## 3. The capability matrix

### 3.1 Verbs

A fixed vocabulary, not free-form strings — a typo in a permission name must
be a startup error, not a silent denial.

| App | Verbs |
|---|---|
| `hosts` | `view`, `edit`, `approve`, `delete` |
| `tasks` | `view`, `run` |
| `baselines` | `view`, `run`, `edit` |
| `automations` | `view`, `edit`, `toggle` |
| `alerts` | `view`, `ack`, `silence` |
| `statuspages` | `view`, `edit` |

Six apps, twenty-one verbs. Small enough to render as one screen per site and
to hold in your head when debugging a denial.

### 3.2 What each role does with it

| Role | Behaviour |
|---|---|
| `OWNER` | everything, everywhere, matrix not consulted |
| `ADMIN` | everything **within their granted sites**, matrix not consulted |
| `OPERATOR` | exactly what the matrix grants, per site |
| `VIEWER` | every `view` verb within their granted sites, nothing else |
| `NONE` | nothing |

Only OPERATOR reads the matrix. Admin and Viewer are all-or-nothing *within*
their scope, which is what keeps the common case simple.

### 3.3 Storage

```python
class SiteCapability(models.Model):
    """One granted verb for one operator in one site. Rows exist only for
    granted capabilities — absence is denial."""
    user_site_role = models.ForeignKey(
        UserSiteRole, on_delete=models.CASCADE, related_name="capabilities")
    app = models.CharField(max_length=20)    # validated against CAPABILITIES
    verb = models.CharField(max_length=20)   # validated against CAPABILITIES

    class Meta:
        unique_together = ("user_site_role", "app", "verb")
```

Rows rather than a JSON blob, so a capability is queryable ("who can approve
hosts at West Campus?") and so `unique_together` makes double-granting a
database error rather than a duplicated list entry.

Hanging capabilities off `UserSiteRole` rather than off `(user, site)`
directly means revoking someone's role in a site takes their capabilities
with it by cascade — there is no way to leave orphaned grants behind.

### 3.4 The check

One function, in `apps/accounts/permissions.py`:

```python
def can(user, site, app, verb) -> bool:
    """The single authorisation question. `site` may be None for unscoped
    areas (accounts, settings, the task library)."""
```

`CAPABILITIES` is a module-level dict of app -> frozenset(verbs). `can()`
raises `ValueError` on an unknown app or verb, so a typo fails loudly in
tests instead of silently denying in production.

---

## 4. Applying it to ~53 call sites

The existing `IsAdmin` / `IsOperator` DRF permission classes stay, but become
thin wrappers over `can()`. Endpoints migrate in three groups:

1. **Unscoped endpoints** (accounts, settings, licensing, task library) —
   pass `site=None`. Mechanical change, no behaviour difference.
2. **Single-object endpoints** (`/hosts/{id}/`, `/alerts/{id}/ack/`) — derive
   the site from the object via `scoping.sites_for_hosts` or the alert's
   host. One helper, `site_of(obj)`, covers all of them.
3. **List endpoints** — filter rather than deny. A list returns what the user
   may see in the sites they may see, and never 403s just because one row
   was out of scope.

Group 3 is where the risk is: a missed filter leaks another site's rows into
a list. §6 covers how that gets tested rather than hoped for.

---

## 5. Licensing

Per-site roles and the capability matrix are Business (`rbac_advanced`,
already an existing feature flag).

Under a lapsed licence, §6 of SQSY-LICENSING applies and nothing blocks:
existing `UserSiteRole` rows keep resolving exactly as they did, because
resolution is not a licensing decision — revoking people's access on lapse
would be a severity-one outage. What lapses is the *editing*: granting a new
per-site role or changing the matrix answers 402. Free installs have one site,
so per-site roles are meaningless there rather than withheld.

---

## 6. Testing

The resolution table gets exhaustive tests — it is six branches and every one
is a security boundary:

| Case | Expected |
|---|---|
| superuser, any site | OWNER |
| no rows at all, is_staff | ADMIN, every site (unchanged from today) |
| no rows at all, no profile | VIEWER (unchanged from today) |
| no rows at all, profile role set | that role, every site |
| row for this site | that role |
| no row here, global row | the global row's role |
| rows elsewhere, none here, no global | NONE |
| owner flag beats every row | OWNER |

Then, per group in §4:

- Every unscoped endpoint still answers identically for an unscoped user.
- Every single-object endpoint denies cross-site access — one test per verb.
- **Every list endpoint gets a leak test**: create two sites with rows in
  each, grant a user one site, assert the other site's rows never appear.
  This is written as a parametrised test over a registry of list endpoints,
  so adding a list endpoint without adding it to the registry fails.
- The lapse test: expire the licence, assert an existing operator still
  resolves and can still act, and that granting a *new* role answers 402.

Baseline before starting: **291 tests pass.**

---

## 7. Build order

Each step leaves the suite green and the app working.

1. `CAPABILITIES` vocabulary and `can()`, with `site=None` behaving exactly
   like today. No call sites change. Pure addition.
2. `role_of(user, site)` with the six-branch table. The existing
   zero-argument `role_of(user)` stays as a wrapper passing `site=None`, so
   no caller changes yet and no migration is needed.
3. `SiteCapability` and the matrix, still unread by any endpoint.
4. Migrate group 1 call sites (unscoped) — should be a no-op behaviourally.
5. Migrate group 2 (single-object).
6. Migrate group 3 (lists) with the leak-test registry.
7. UI: the per-user site/role grid and the per-site capability matrix,
   including the narrowing warning from §1.1.

Steps 1-3 are additive and safe to land together. Step 6 is the one that can
leak data and deserves its own review.

---

## 8. Out of scope

- Named permission sets / reusable bundles. Considered and deferred: with
  six apps the matrix is small enough that indirection costs more than it
  saves. Revisit if sites exceed ~10.
- Per-object permissions (naming individual baselines a user may run).
- The site tile grid and sticky site context.
