# Remote reprovisioning — design (2026.6.0)

Rebuild a machine's operating system from the Vigil console: pick an image and
a profile, pass a three-factor confirmation, and the host wipes itself, installs
unattended, re-enrols its agent against the same `Host` row, takes a tag you
chose, and optionally runs a baseline. A compromised or drifted server goes back
to known-good without anyone walking to it.

Free feature. Lives in `server/apps/reprovision/` under AGPLv3.

---

## 1. Scope

**In, for 2026.6.0:** Ubuntu (subiquity autoinstall), Debian (preseed), and the
RHEL family — Rocky, Alma, RHEL, Fedora — (kickstart). Image catalog, install
profiles, the confirmation ceremony, the rebuild job state machine, agent-side
pre-flight and staging, identity-preserving re-enrolment, completion tagging,
and baseline chaining.

**Out, deliberately:** Windows. It shares almost nothing with the Linux path —
`unattend.xml` instead of kickstart, WinPE staged via `bcdedit` instead of a
GRUB one-shot entry, a different agent installer. It is planned for 2026.7 or
2026.8, and §12 records the seams that keep it a drop-in rather than a rewrite.

**Out, on the evidence:** PXE/netboot and out-of-band BMC (Redfish/IPMI). Both
were considered and rejected for this release — PXE needs control of DHCP
options 66/67 and per-machine firmware changes, which is a heavy ask for a
1–50 device product; BMC needs enterprise hardware most of the target fleet
does not have. §11 records the limitation that decision leaves behind.

## 2. Inherited constraints

These come from `CLAUDE.md` and are not up for renegotiation here.

- **Agent-based, outbound-only.** No inbound connection to a monitored host,
  ever. Everything below rides the existing check-in/task-result channel.
- **The agent's mode is authoritative.** A compromised server cannot escalate
  an agent's permissions. §4.1 is the whole design consequence of this.
- **Structured actions, never raw commands.** New action types go in
  `ACTION_REGISTRY` and are mirrored by the agent executor.
- **Nothing ever blocks.** Reprovisioning is Free, so no licensing path touches
  it at all — no `require_feature`, no 402, nothing to degrade.
- **Extension tables FK into core; core gains no columns.** That rule governs
  `apps_business`. `apps/reprovision` is core, so the one core column this adds
  (§6, `Host.maintenance_until`) is legitimate — but it is the only one.

## 3. Architecture

### 3.1 Mechanism

The agent, running on the live OS, stages an installer kernel and initrd into
`/boot`, writes a **one-shot** bootloader entry, and reboots. The installer
boots, fetches a generated answer file from Vigil, wipes the disk, installs
unattended, and its post-install stage runs Vigil's `install.sh` with a
one-time enrolment token.

**One-shot bootloader entry, not `kexec`.** `kexec` is faster and skips
firmware, but it has no fallback: if the installer kernel fails to come up, the
machine is simply down. A one-shot entry (`grub-reboot` / `grub2-reboot` writing
`next_entry` to grubenv, or `bootctl set-oneshot`) lapses on its own, so a
failed installer boot lands back in the old OS unaided. That single property
converts the scariest failure class into a self-healing one and is worth more
than the seconds `kexec` saves.

Supported bootloaders: GRUB2 (BIOS and UEFI) and systemd-boot. Anything else
fails pre-flight rather than being guessed at.

### 3.2 The point of no return

**The answer-file fetch is the only positive proof the installer booted, and it
is also the moment the disk starts being destroyed.** The entire state machine
is organised around that line.

Before it, nothing is committed: `/boot` has some extra files and a one-shot
entry that expires. After it, the machine is being wiped and remote recovery is
no longer possible. Every design decision below asks which side of the line it
is on.

### 3.3 Lifecycle

```
PENDING ──60s abort window──► STAGING ──► STAGED ──► REBOOTING
   │                             │           │           │
   └──────── ABORTED ────────────┴───────────┘           │
                                                          │
                     old agent checks in ─────────────► FAILED
                     (installer never booted)             │
                                                          │
                          ═══════ POINT OF NO RETURN ═════╪═══
                                                          │
                                                     INSTALLING
                                                          │
                                                     ENROLLING
                                                          │
                                                     COMPLETED
```

| State | Meaning | Exit |
|---|---|---|
| `PENDING` | Ceremony passed; 60s abort window open | 60s elapse → `STAGING`; operator aborts → `ABORTED` |
| `STAGING` | Agent fetching kernel/initrd, verifying checksums | success → `STAGED`; any error → `FAILED`; operator aborts → `ABORTED` |
| `STAGED` | Artifacts on disk, pre-flight green, bootloader untouched | commit dispatched → `REBOOTING`; operator aborts → `ABORTED` |
| `REBOOTING` | One-shot entry written, reboot issued | answer fetched → `INSTALLING`; old agent check-in → `FAILED`; deadline → `TIMED_OUT` |
| `INSTALLING` | Installer confirmed running; disk being wiped | enrol token redeemed → `ENROLLING`; deadline → `TIMED_OUT` |
| `ENROLLING` | New agent registered, awaiting first check-in | check-in → `COMPLETED`; deadline → `TIMED_OUT` |
| `COMPLETED` | Tag applied, maintenance cleared, baseline dispatched | terminal |
| `FAILED` / `ABORTED` / `TIMED_OUT` | With a recorded reason | terminal |

The 60-second `PENDING` window implements the high-risk convention
(2FA + 60s delay) as a real abort button rather than a spinner. `ABORTED` is
only reachable before the reboot is issued; after that the honest answer is
that it is out of our hands. Aborting from `STAGING` or `STAGED` dispatches a
cleanup action removing `/boot/vigil-reprovision/`; no bootloader entry exists
yet at that point, since only `reprovision_commit` writes one. An abandoned job
leaves nothing behind.

A Celery beat task sweeps for jobs past their deadline and moves them to
`TIMED_OUT`. Default deadline: 60 minutes from `REBOOTING`, configurable per
profile, because a RHEL install over a slow link legitimately takes longer than
a Debian netinstall.

## 4. Security

This is the highest-consequence feature in Vigil: it destroys data by design,
and no revocation undoes it after the fact. Security is therefore the spine of
the design, not a review pass over it.

### 4.1 `reprovision` is carved out of the mode system

`AgentConfig.task_allowed()` returns `True` for **any** action when
`mode: full_control`. A naive `reprovision` action would therefore mean:
compromise the Vigil server, and every full-control host in the fleet wipes
itself on command. That directly contradicts the standing principle that a
compromised server cannot escalate an agent's permissions — and a rebuild is
the ultimate escalation.

**So the reprovision actions are exempt from mode entirely.** They require a
separate explicit opt-in in `agent.yml`:

```yaml
# Permit this machine to be remotely wiped and reinstalled.
# Not implied by full_control. Off by default, everywhere.
allow_reprovision: false
```

`task_allowed()` gains an explicit branch: reprovision actions consult
`allow_reprovision` and nothing else — not mode, not the allowlist. The
authority to wipe a machine lives on that machine.

The install one-liner can set it at enrolment time, so adoption is a flag, not
a chore. The read-only `reprovision_preflight` probe is **not** covered by the
carve-out — it changes nothing, so it behaves as an ordinary low-risk
allowlistable action, which lets an operator check rebuild-readiness across a
fleet without arming anything.

### 4.2 The answer file

It is fetched by an installer that holds no credentials, so an unguessable URL
is the only available gate: `secrets.token_urlsafe(32)`, 256 bits. Rather than
rely on that alone, the file is made **not worth stealing**:

- **Root password stored only as a crypt hash**, never plaintext. SSH keys are
  the documented preference. Profile secrets are encrypted at rest with the
  existing `apps/hosts/crypto.py` Fernet helper — the same one behind
  `ADConfig.bind_password_encrypted`.
- **The enrolment token is one-time and job-scoped.** Redeeming it grants
  exactly one thing: become this one existing host. It cannot enrol a second
  machine and cannot outlive the job deadline. Stored hashed, so a database
  read does not yield a usable token.
- **Rendered lazily on fetch**, never written to disk. Served `Cache-Control:
  no-store`. Never logged in full.
- **Servable only while the job is in `REBOOTING` or `INSTALLING`.** Outside
  that window the URL 404s, including after the job completes.
- **Repeat fetches are allowed but pinned.** Strict single-fetch would break
  real installers — subiquity re-reads its data source — so subsequent fetches
  are served only to the IP that made the first one, and each is audited.

A full interception yields a crackable password hash, public SSH keys, and a
token that is already consumed. Not nothing; not a foothold.

**Transport.** If `VIGIL_PUBLIC_URL` is plain HTTP, that file crosses the LAN
in the clear. This is not hard-blocked, because that would break exactly the
homelab setups Vigil targets — but the ceremony surfaces an explicit warning
that must be acknowledged (`acknowledge_plaintext_transport`), and the
acknowledgement is recorded in the audit event. The insecure path stays
available and stops being silent.

### 4.3 Credential rotation and revocation

At the transition into `INSTALLING` the **old `agent_token` is revoked**.
Without this, a token recovered from a backup or forensic image of the wiped
disk authenticates as that host indefinitely.

The handoff: post-install runs `install.sh` with a one-time
`VIGIL_ENROLL_TOKEN`. The new agent generates its own `agent_token` as it
normally does and POSTs both to `/api/v1/reprovision/enroll`. The server:

1. Looks the job up by **token hash**, rejecting if consumed, expired, or the
   job is not in `INSTALLING`/`ENROLLING`.
2. Consumes the token **atomically** — a single conditional
   `UPDATE ... WHERE consumed_at IS NULL` returning a rowcount, not
   read-then-write — so a replay race cannot mint two approved hosts.
3. Rotates `agent_token` on the existing `Host` row, sets status `APPROVED`,
   stamps `rebuilt_at`.
4. **Clears stale `HostInventory`, `DockerContainer` rows, and vuln findings.**
   They describe an operating system that no longer exists; leaving them means
   the dashboard reports packages and CVEs for a dead install.

The endpoint is throttled per IP exactly as `hosts.register()` already is.

### 4.4 Authorization

The ceremony requires password **and** TOTP **and** the typed hostname — all
three. The existing `require_totp_confirmation` verifies **only** TOTP — the
`"password": "..."` in `definition_deploy`'s docstring is stale and is never
read — so rebuild gets its own confirmation function rather than reusing that
one, and adds a real password check on top. Re-authentication
happens at confirm time regardless of session age: an idle hijacked session
must not be able to rebuild a fleet.

`CAPABILITIES` in `apps/accounts/permissions.py` gains:

```python
"reprovision": frozenset({"view", "rebuild"}),
```

grantable per-site through the operator matrix from 2026.5.0. Admins get both.

**Image management is admin-only and deliberately absent from the operator
matrix.** Whoever can upload an ISO chooses what code runs as root on every
rebuilt machine — the same reasoning that already makes `upload_agent`
admin-only. There is no per-site delegation of it.

Images require a SHA-256 at registration, verified after fetch, and displayed
in the ceremony next to the target disk.

### 4.5 Audit

Every state transition emits through `vigil/hooks.py` rather than importing
`apps_business.audits` directly — reprovision is Free, audits is Business, and
the event bus is exactly the seam for that. Events carry: actor, source IP,
which auth factors were satisfied, image name and checksum, profile, target
disk, and every answer-file fetch with its source IP.

## 5. Pre-flight

Dispatched before staging; its snapshot is stored on the job. Any failure
aborts before the bootloader is touched.

| Check | Fails when |
|---|---|
| Bootloader family | Not GRUB2 (BIOS/UEFI) or systemd-boot |
| `/boot` free space | Less than kernel + initrd + 20% headroom |
| RAM | Below the installer's documented minimum for the family |
| Architecture | Host arch ≠ image arch |
| Target disk present | Named device absent |
| Network match | Profile is static and its subnet ≠ the host's current address |

The disk check reports **every** detected disk with size and current mount
points, not just the target — because the failure this guards against is
selecting the data disk by mistake, and that is caught by a human reading the
list, not by a predicate.

The network check is the one that earns its place: the dominant cause of
"installed fine, never came back" is a static profile that does not match the
network the machine is actually on. It can be overridden explicitly for a
deliberate re-addressing, and the override is audited.

## 6. Data model

`apps/reprovision/models.py`:

**`OSImage`** — `name`, `os_family` (`ubuntu`/`debian`/`rhel`), `version`,
`architecture`, `sha256`, `size_bytes`, `status`
(`importing`/`ready`/`failed`), `import_error`, `kernel_path`, `initrd_path`,
`tree_path`, `created_by`, `created_at`.

On import Vigil reads the ISO with **`pycdlib`** — a pure-Python ISO9660
reader, because a Docker container cannot loop-mount — extracts kernel and
initrd, unpacks the install tree, verifies the checksum, then **discards the
ISO and keeps the checksum**. Storage stays at roughly 1× per image rather
than 2×. The trade-off is that the original cannot be re-verified later; it is
verified once, at import, and the digest is recorded.

**`InstallProfile`** — `name`, `image` FK, `disk_target`, `partition_scheme`,
`filesystem`, `network_mode` (`dhcp`/`static`) with `static_address`,
`gateway`, `dns`, `timezone`, `locale`, `keyboard`, `admin_username`,
`admin_password_encrypted`, `ssh_authorized_keys`, `extra_packages` (JSON
list), `raw_append`, `deadline_minutes`, `created_by`.

`raw_append` is the escape hatch: appended verbatim to the rendered answer
file for anything the typed fields do not model.

**`RebuildJob`** — `host` FK, `image` FK, `profile` FK, `requested_by`,
`requested_at`, `confirmed_ip`, `state`, `state_changed_at`, `deadline`,
`answer_token_hash`, `answer_fetched_at`, `answer_fetch_ip`,
`enroll_token_hash`, `enroll_consumed_at`, `preflight` (JSON snapshot),
`completion_tag`, `post_baseline` FK (nullable), `post_run` FK to `TaskRun`
(nullable), `failure_reason`.

The job holds the `TaskRun` FK, so `apps/tasks` needs no new column —
`TaskRun.Source` gains a `REPROVISION` choice and nothing else.

**Core change (the only one):** `Host.maintenance_until`, nullable datetime,
honoured by the alert evaluator. A rebuild means ~40 minutes of "host down";
without suppression the first real rebuild pages everyone and teaches people to
ignore the alerts. `apps/alerts` has no maintenance-window concept today. The
job sets it entering `REBOOTING` and clears it on every terminal state.

Jobs are site-scoped through their host via the existing `vigil.scoping`
façade, which degrades correctly when `apps_business` is absent. The image and
profile catalog is global in 2026.6.0; per-site catalogs can be added later
with an assignment table exactly as baselines were, and are not built now.

## 7. Answer-file rendering

One renderer per family behind a common interface, registered by `os_family`
so a fourth can be added without touching callers.

| Family | Kernel cmdline | Endpoint shape |
|---|---|---|
| Ubuntu | `autoinstall ds=nocloud-net;s=<base>/reprovision/answer/<tok>/` | Directory: serves `user-data` and `meta-data` |
| Debian | `auto=true priority=critical url=<base>/reprovision/answer/<tok>` | Single preseed file |
| RHEL | `inst.ks=<base>/reprovision/answer/<tok> inst.repo=<base>/reprovision/tree/<image_id>/` | Single kickstart file |

Each renderer emits the Vigil enrolment block into the family's post-install
stage (`late-commands`, `d-i preseed/late_command`, `%post`), invoking
`install.sh` with the one-time token and the server URL.

**Golden-file tests are mandatory here.** A renderer regression produces a
machine that wipes itself and then hangs at an interactive prompt — this
feature's worst outcome — so each renderer is pinned byte-for-byte against a
committed expected output.

## 8. Agent-side changes

New module `agent/vigil_agent/reprovision.py`. Four actions, mirrored in
`ACTION_REGISTRY`:

- **`reprovision_preflight`** — risk `low`, allowlistable, read-only. Returns
  the §5 snapshot.
- **`reprovision_stage`** — risk `high`, requires `allow_reprovision`.
  Downloads kernel and initrd, verifies SHA-256, writes them under
  `/boot/vigil-reprovision/`. Reversible: touches nothing but those files.
- **`reprovision_commit`** — risk `high`, requires `allow_reprovision`. Writes
  the one-shot bootloader entry and reboots.
- **`reprovision_cleanup`** — risk `low`, requires `allow_reprovision`. Removes
  `/boot/vigil-reprovision/`. Dispatched on abort, and on `FAILED` when the old
  OS is still reachable.

Staging and committing are separate on purpose: `STAGED` is a genuine
checkpoint where the slow, failure-prone work is already done and the operator
can still abort with nothing damaged.

## 9. API surface

```
GET    /api/v1/reprovision/images/                list catalog
POST   /api/v1/reprovision/images/                register (admin only)
GET    /api/v1/reprovision/images/{id}/           detail + import status
DELETE /api/v1/reprovision/images/{id}/           admin only

GET    /api/v1/reprovision/profiles/              list
POST   /api/v1/reprovision/profiles/              create
GET|PATCH|DELETE /api/v1/reprovision/profiles/{id}/
POST   /api/v1/reprovision/profiles/{id}/preview/ render for a host, secrets redacted

POST   /api/v1/hosts/{id}/preflight/              dispatch the probe
GET    /api/v1/reprovision/jobs/                  list, site-scoped via host
POST   /api/v1/reprovision/jobs/                  create — the ceremony
GET    /api/v1/reprovision/jobs/{id}/             detail + timeline
POST   /api/v1/reprovision/jobs/{id}/abort/       PENDING | STAGING | STAGED only

GET    /reprovision/answer/<opaque>               installer-facing, unauthenticated
GET    /reprovision/tree/<image_id>/...           installer-facing, unauthenticated
POST   /api/v1/reprovision/enroll                 one-time token, throttled per IP
```

The two installer-facing routes sit outside `/api/v1` and outside session auth
by necessity — the installer has no credentials. Their security is §4.2.

Ceremony payload:

```json
{
  "host": "<uuid>", "image": "<uuid>", "profile": "<uuid>",
  "completion_tag": "rebuilt:need config",
  "post_baseline": "<uuid>|null",
  "password": "...", "totp": "123456",
  "typed_hostname": "web-01",
  "acknowledge_plaintext_transport": true
}
```

## 10. Completion chain

On the first check-in from the rebuilt agent the job:

1. Applies the free-form `key:value` completion tag, **rejecting the reserved
   `agent:` namespace** so it cannot collide with agent-advertised tags.
   Operator-set tags from before the rebuild survive; this appends.
2. Clears `Host.maintenance_until`.
3. Dispatches the chosen baseline via the existing `dispatch_to_host`, records
   the `TaskRun` on the job, and stamps `COMPLETED`.

`rebuilt:need config` → baseline reinstalls the stack → compromised box back to
known-good, with the whole path visible in the run history shipped in 2026.5.0.

## 11. Failure modes

| What breaks | Where | Outcome |
|---|---|---|
| Pre-flight fails | before staging | `FAILED`, nothing touched |
| Download or checksum mismatch | staging | `FAILED`, bootloader untouched |
| Installer never boots | after reboot | one-shot entry lapses, old OS returns, old agent checks in → `FAILED` |
| Install dies mid-way | after the line | `TIMED_OUT` — **needs physical access** |
| Installed, agent never enrols | after the line | `TIMED_OUT`, machine alive but invisible |

Rows four and five are the irreducible risk and the docs will say so plainly.
Row five is engineered against by the pre-flight network check (§5); row four
is not recoverable by any means available to an agent-driven design.

**The limitation this design accepts.** If a host is genuinely rooted, the
agent on it is rooted too, and an attacker with kernel-level persistence can
no-op the wipe while reporting success. Agent-driven reinstall is excellent for
drift, ransomware cleanup, and returning a box to known-good; it is **not** a
trustworthy eradication path against a sophisticated attacker. What proves the
wipe happened is the post-rebuild evidence — a rotated `agent_token`, a fresh
`rebuilt_at`, cleared inventory — not the agent's own success report. Users
who need guaranteed eradication need out-of-band reinstall, which is the BMC
path deferred in §1. The documentation must say this rather than implying the
feature is an incident-response silver bullet.

## 12. Windows readiness

Windows is not built here, but three seams keep it additive:

1. **Renderer registry keyed by `os_family`** — `unattend.xml` becomes a fourth
   registration, not a branch in shared code.
2. **Boot staging behind an interface** — `stage()` / `commit()` are agent-side
   operations with family-specific implementations, so WinPE + `bcdedit`
   replaces GRUB one-shot without changing the job, the ceremony, or the state
   machine.
3. **Enrolment is transport-agnostic** — the one-time token exchange is a plain
   HTTPS POST, so `install.ps1` uses the identical endpoint.

Nothing in the catalog, ceremony, RBAC, or re-enrolment design is Linux-specific.

## 13. Testing

- **Renderers:** golden-file, byte-for-byte, per family. Non-negotiable (§7).
- **State machine:** every transition and every timeout path.
- **Security:**
  - answer file 404s outside `REBOOTING`/`INSTALLING`, including after completion
  - second fetch from a different IP is refused
  - concurrent double-redeem of an enrolment token yields exactly one winner
  - a `full_control` agent without `allow_reprovision` refuses the action
  - old `agent_token` stops authenticating once `INSTALLING` is reached
  - each RBAC denial path, including operator without `rebuild`
  - ceremony rejects a correct password with a wrong hostname, and vice versa
- **Pre-flight:** each check's failure path, and the network-mismatch override.
- **Completion chain:** tag applied, `agent:` namespace rejected,
  `maintenance_until` cleared, baseline dispatched.
- **AGPL probe:** `vigil/_agpl_probe.py` extended, since jobs are site-scoped
  through the scoping façade.

**The gap, named rather than papered over:** no CI job can test an actual wipe.
Everything above the boot boundary is covered; the boot-and-install itself
needs a real VM. That becomes a documented manual smoke procedure — one per OS
family, run before tagging — not something claimed as automated coverage. The
migration failure earlier in this release cycle is the standing reminder that a
green suite proves only what it actually exercises: the whole 338-test run
passed on SQLite while the migration was broken on PostgreSQL.

## 14. Deliberate deferrals

- **Windows** — 2026.7/2026.8, seams in §12.
- **BMC/Redfish and PXE** — the eradication and dead-box paths, §1 and §11.
- **Per-site image catalogs** — global in 6.0; the assignment-table pattern
  from baselines applies when needed.
- **GPG signature verification of distro images** — SHA-256 plus admin-only
  upload is the floor for 6.0.
- **Bulk rebuild** — one host per job. Fleet-wide rebuild multiplies the blast
  radius of every failure mode above and deserves its own design.
