# Agent privileges: what each mode runs as, and what it costs

The Vigil agent gives up privilege it does not need. A monitoring agent reads
`/proc` and performance counters; it does not need to be root or SYSTEM to do
that, and on most fleets the overwhelming majority of hosts are only ever
monitored. So **monitor mode runs unprivileged on both Linux and Windows**, and
the modes that execute tasks — which genuinely do need privilege — run
privileged and say so at install time.

Switching between them costs one line of config and a re-run of the installer.
No commands to learn, nothing to undo.

## What each mode runs as

| | Linux | Windows |
|---|---|---|
| **monitor** | `vigil-agent` (system user, `nologin`) | `NT SERVICE\vigil-agent` (virtual account) |
| **managed** | root | `LocalSystem` |
| **full_control** | root | `LocalSystem` |

On Linux, monitor mode additionally runs under systemd hardening:

```
User=vigil-agent
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
CapabilityBoundingSet=            # empty — no capabilities at all
ReadWritePaths=/var/lib/vigil-agent
```

Verified on a running agent rather than read off the unit file — `/proc/<pid>/status`
reports `Uid: 997 997 997 997` (no route back to root through any of the four),
`CapEff: 0000000000000000`, and `NoNewPrivs: 1`. The binary is root-owned `0755`,
so the agent cannot rewrite its own executable.

On Windows, `NT SERVICE\vigil-agent` is a **virtual account**: the Service
Control Manager creates and manages it, there is no password stored anywhere,
and it is scoped to this one service rather than shared across services the way
`LocalService` is. It is granted read on `agent.yml`, read+execute on the
install tree, and modify on its own data directory — nothing else.

## What monitor mode cannot do

This is the trade-off, stated plainly. Both of these are verified behaviours,
not predictions.

**Windows Update status is unavailable.** The Windows Update COM API refuses an
unprivileged service account:

```
com_error: (-2147352567, 'Exception occurred.',
            (0, None, None, None, 0, -2147024891), None)
                                   ^^^^^^^^^^^^ E_ACCESSDENIED
```

So `windows_updates` stays empty on a monitor-mode Windows host. The agent
reports *no summary* rather than zero — being told "0 updates pending" by a
machine that could not look is worse than being told nothing — and logs the
reason once per start rather than a traceback every cycle. Ordinary metrics are
unaffected.

**Reprovision cannot stage.** `ProtectSystem=strict` makes `/boot` read-only
*even for root*, so a rebuild fails at the staging step:

```
[Errno 30] Read-only file system: '/boot/vigil-reprovision'
```

Reprovision also requires `allow_reprovision: true`, which is deliberately not
an allowlist entry — see below.

**Task execution generally.** Monitor mode executes nothing at all, by design.
That is the whole point of it.

## Switching modes

Edit `mode:` in `agent.yml`, then **re-run the installer**:

```bash
# Linux / macOS
curl -fsSL https://vigil.example.com/agent/install.sh | sudo bash
```

```powershell
# Windows
irm https://vigil.example.com/agent/install.ps1 | iex
```

The installer reads the mode out of the existing `agent.yml` and regenerates
the service definition to match. It does not overwrite your config or your
token.

### Do not hand-edit the unit file

The agent warns when the mode needs privilege and the process does not have it.
That warning used to advise removing the `User=` line from the systemd unit,
which **is not sufficient** — the monitor-mode unit also carries
`ProtectSystem=strict`, and under that `/boot` and `/etc` stay read-only for
root:

```
# systemd-run --property=ProtectSystem=strict /bin/sh -c 'id -u; mkdir -p /boot/x'
0
mkdir: Read-only file system
# systemd-run /bin/sh -c 'mkdir -p /boot/x'      # control
WRITE_OK
```

An operator following that advice got a root agent that still could not do the
work, failing one task at a time with the cause two files away. Re-running the
installer is the complete fix and the only one worth documenting.

## The mode is the agent's, not the server's

`CLAUDE.md` states it as a rule: *the agent's mode is authoritative — a
compromised server cannot escalate an agent's permissions.* In practice the
server re-reads the mode from the agent on every check-in, so an edit made only
in the server's database survives at most one interval and grants nothing.

Verified against a live agent by simulating a compromised server — setting
`Host.mode` to `full_control` in the database while `agent.yml` still said
`monitor`:

```
DB now: full_control
  t+5s   DB mode = full_control
  t+10s  DB mode = full_control
  t+15s  DB mode = full_control
  t+20s  DB mode = monitor        <- the agent's next check-in overwrote it
```

A host can therefore misrepresent itself to the server, but it cannot be made
to execute anything its own config does not permit.

## Reprovision is gated separately

The three destructive reprovision actions (`reprovision_stage`,
`reprovision_commit`, `reprovision_cleanup`) are **not allowlistable**. Naming
one in `allowlist:` draws an "ignoring unknown action" warning rather than
silently appearing to work. They are granted only by:

```yaml
allow_reprovision: true
mode: managed          # or full_control — never monitor
```

Both are required. `reprovision_preflight` is read-only and is a normal
allowlist entry.
