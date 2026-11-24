# Agent privileges

The Vigil agent runs with only the privileges its mode needs. Monitoring needs
very little, so **monitor mode runs without privileges on both Linux and
Windows**. The modes that run tasks need more access, so they run with higher
privileges. The installer tells you which privileges a mode uses.

You change a mode by editing one line in the agent's config and running the
installer again. You do not need to undo anything first.

## What each mode runs as

| | Linux | Windows |
|---|---|---|
| **monitor** | `vigil-agent` (system user, `nologin`) | `NT SERVICE\vigil-agent` (virtual account) |
| **managed** | root | `LocalSystem` |
| **full_control** | root | `LocalSystem` |

On Linux, monitor mode also runs with these systemd settings:

```
User=vigil-agent
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
CapabilityBoundingSet=            # empty, no capabilities at all
ReadWritePaths=/var/lib/vigil-agent
```

The agent binary is owned by root with mode `0755`, so the agent cannot change
its own program file.

On Windows, `NT SERVICE\vigil-agent` is a virtual account. Windows creates and
manages it, and no password is stored. It has read access to `agent.yml`,
read and run access to the install folder, and modify access to its own data
folder. It has no other access.

## What monitor mode cannot do

**Windows Update status is unavailable.** The Windows Update interface refuses
requests from an unprivileged service account. On a monitor-mode Windows host,
the `windows_updates` list stays empty. The agent reports no summary rather
than "0 updates pending", because a machine that cannot check should not
report zero. The agent logs the reason once each time it starts. Other metrics
work as normal.

**Reprovisioning cannot stage.** `ProtectSystem=strict` makes `/boot` read-only,
even for root. A rebuild fails at the staging step with a `Read-only file
system` error. A rebuild also requires `allow_reprovision: true` in the agent
config. See [Reprovisioning](reprovisioning.md).

**Tasks.** Monitor mode runs no tasks.

## Containers (Docker and Podman)

Vigil talks to the container engine through its Unix socket. It does not use
the `docker` group, because that group gives root access.

- **managed** and **full_control** run as root and open the engine socket
  directly. The sockets are `/var/run/docker.sock`, rootful Podman's
  `/run/podman/podman.sock`, or each user's rootless Podman socket under
  `/run/user/<uid>/podman/`. Actions on a rootless socket run through that
  user's engine, not through root's.
- **monitor** runs without privileges and cannot open the engine socket. When
  the installer finds an engine, it also installs `vigil-engine-proxy.service`.
  This service runs the same binary as root with `--engine-proxy`. It listens
  on `/run/vigil/engine-ro.sock` (mode `0600`, owned by `vigil-agent`).

  The proxy allows only these read-only requests: the container list, one
  container's details, one stats reading, logs without `follow`, the image
  list, and the engine's ping and version. The proxy answers every POST,
  DELETE, exec, archive, volume, and network request with `403`. Those requests
  never reach the engine.

Every container action is a signed task. The web interface does not give
anyone direct access to the socket.

## Switch modes

1. Edit `mode:` in `agent.yml`.
2. Run the installer again.

```bash
# Linux / macOS
curl -fsSL https://vigil.example.com/agent/install.sh | sudo bash
```

```powershell
# Windows
irm https://vigil.example.com/agent/install.ps1 | iex
```

The installer reads the mode from the existing `agent.yml` and rewrites the
service definition to match. It keeps your config and your token.

### Do not edit the service file by hand

The agent logs a warning when its mode needs privileges that the process does
not have. Do not fix this by removing the `User=` line from the systemd unit.
The monitor unit also sets `ProtectSystem=strict`, which keeps `/boot` and
`/etc` read-only even for root. The agent would still fail its tasks.

Run the installer again instead. It rewrites the unit file correctly.

## The agent's mode is enforced on the agent

The agent reads its own mode from `agent.yml`. The server cannot raise an
agent's permissions. The server reads the mode from the agent at each check-in.
If you change the mode in the server's database only, the agent's next
check-in overwrites it. The change does not grant any new permissions.

A host can report the wrong mode to the server. It cannot be made to run
anything its own config does not allow.

## Inline scripts and hash approval

A task can contain its script as text, using an `execute_script` action with a
`shell:` and a `script:` block, instead of a file on disk. The host's mode
decides whether the script runs:

- **full_control** runs any inline script. No approval is needed.
- **managed** runs an inline script only if its exact hash is listed in
  `allowed_script_hashes` in `agent.yml`. The agent computes the hash of the
  script it received. If the hash is not listed, the agent refuses the script
  and shows the hash it expected.
- **monitor** never runs scripts.

Any change to a script changes its hash. You must approve the new hash on each
managed host before the edited script runs there. Approving a script approves
its exact contents only. It does not allow the task to run other scripts.

To approve a script, run one of these commands on the host:

```bash
# copy the hash shown in the task editor
sudo vigil-agent allow-script --hash sha256:<hex>

# or let it hash the file for you
sudo vigil-agent allow-script path/to/script.sh
```

Either command adds the hash to `agent.yml`:

```yaml
allowed_script_hashes:
  - sha256:<64 hex>
```

To compute the hash yourself, run `sha256sum script.sh` on Linux or macOS, or
`(Get-FileHash -Algorithm SHA256 .\script.ps1).Hash.ToLower()` on Windows. Then
add the `sha256:` prefix. The file must use LF line endings and end with
exactly one newline. Vigil normalizes the script this way before it hashes it.
A different line ending or an extra blank line at the end gives a different
hash. `vigil-agent allow-script` does this for you, so use it when you can.

There is no setting that allows inline scripts in general. An inline script runs
the same way as `run_command`, except the command text comes from the server
instead of a file. To allow any inline script on a host, set that host to
`full_control`.

## Reprovisioning needs its own setting

The three rebuild actions, `reprovision_stage`, `reprovision_commit`, and
`reprovision_cleanup`, cannot be added to the allowlist. If you list one in
`allowlist:`, the agent logs an "ignoring unknown action" warning and does not
run it.

These actions run only when both settings are present:

```yaml
allow_reprovision: true
mode: managed          # or full_control, never monitor
```

`reprovision_preflight` only reads information. You can add it to the
allowlist like any other action.

## Code signing on Windows

The Windows agent is not signed. Microsoft Defender did not flag it when we
scanned it on Windows 11 with real-time protection on. That result describes
the current build, and a later build may behave differently.

Without a signature:

- **SmartScreen** warns when someone downloads the agent in a browser and runs
  it. The `irm ... | iex` command is not affected, because the file is not
  written to disk with a download mark.
- **WDAC and AppLocker** policies often block unsigned programs. In an
  environment that enforces one of these policies, the agent cannot be
  installed.
- **PyInstaller output** is often reported as a false positive by antivirus
  software.

Release builds can be signed when the repository has these secrets:

```
WINDOWS_CERT_PFX_BASE64    # base64 of the .pfx
WINDOWS_CERT_PASSWORD
```

When the secrets are set, the build signs the binary with SHA-256 and an
RFC 3161 timestamp. It then checks the signature. Without the secrets, the build
logs a notice and continues without signing.

The signing step runs before the zip file is created. The SHA-256 published
for the zip covers the signed binary. `install.ps1` checks that value.
