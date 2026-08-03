# Remote reprovisioning — operator runbook

Rebuild a machine's operating system from the Vigil console. Pick an image and
a profile, pass a three-factor confirmation, and the host wipes itself,
installs unattended, re-enrols its agent against the same host record, takes a
tag you chose, and optionally runs a baseline.

Design notes live in `docs/reprovisioning.md`. This page is what you need to
run one.

---

## Read this first

**Rebuild destroys everything on the target disk.** There is no undo once the
installer starts. The point of no return is the moment the installer fetches
its answer file — before that, aborting is safe and leaves the machine
untouched; after it, the disk is being rewritten.

**If the install dies part-way, the machine needs physical access.** No remote
recovery exists past that line. Vigil will report the job as timed out and say
so, rather than pretending otherwise.

**This is not a guaranteed eradication path.** If a host is genuinely rooted,
the agent on it is rooted too, and an attacker with kernel-level persistence
can no-op the wipe and report success. Rebuild is excellent for drift,
ransomware cleanup, and returning a box to known-good. It is not proof that a
sophisticated attacker is gone. What evidences a real rebuild is the
post-rebuild state — a rotated agent token, cleared inventory, a fresh install
fingerprint — not the agent's own success report. Guaranteed eradication needs
out-of-band reinstall, which Vigil does not yet offer.

**Serve Vigil over HTTPS.** The answer file carries the admin password hash,
your SSH keys, and the one-time enrolment token. Over plain HTTP that crosses
the network in the clear. Vigil will not stop you — homelab installs are a
real case — but the ceremony makes you tick a box saying you understand, and
records that you did.

---

## One-time setup

### 1. Allow the machine to be rebuilt

On each host you want to be rebuildable, set this in `agent.yml`
(`/etc/vigil/agent.yml` on Linux) and restart the agent:

```yaml
allow_reprovision: true
```

This is **not** implied by `mode: full_control`, and it cannot be granted
through the allowlist. The authority to destroy a machine lives on that
machine, so a compromised Vigil server cannot order a fleet to wipe itself.

The read-only readiness probe is separate — allowlist `reprovision_preflight`
if you want to check rebuild-readiness across a fleet without arming anything.

### 2. Give Vigil somewhere to keep images

Extracted install trees are several gigabytes each. Point `VIGIL_IMAGE_ROOT`
at a volume sized for them (default `/var/lib/vigil/images`) and make sure the
web and worker containers share it.

### 3. Register an image

**Settings → OS Images.** Admin-only, deliberately: whoever registers an image
chooses what runs as root on every machine rebuilt from it. This is the same
reasoning that makes agent-binary upload admin-only, and it is not delegable
through the per-site operator matrix.

Give the SHA-256 published by the distribution. Vigil verifies it on import,
extracts the installer kernel, initrd, and install tree, then **discards the
ISO** — storage stays at roughly one copy, and the digest is the record that
the bytes were what they claimed to be. A failed import lands on `failed` with
the reason shown, and can never be selected for a rebuild.

Supported families: Ubuntu (subiquity autoinstall), Debian (preseed), and the
RHEL family — Rocky, Alma, RHEL, Fedora (kickstart). Windows is planned for a
later release.

### 4. Build a profile

A profile is the settings for one image: disk target, partitioning,
filesystem, network, timezone, admin account, SSH keys, extra packages.

Two rules Vigil enforces at creation, because discovering either after the
disk is wiped is too late:

- **An SSH key or a password hash is required.** Neither means a machine
  nobody can log into.
- **Static networking needs an address and a gateway.**

`raw_append` is the escape hatch: whatever you put there is appended verbatim
to the rendered answer file, after everything Vigil generated.

---

## Running a rebuild

From the host's detail drawer, **Rebuild OS…**.

1. **Check the summary.** Target disk and image checksum are shown above the
   confirmation fields, and the readiness probe lists *every* disk it found
   with size and current mount points. Read that list. Selecting the data disk
   by mistake is caught by a human here, not by any check Vigil can make.
2. **Pick a completion tag** — e.g. `rebuilt:need config`. It is applied when
   the rebuilt agent first checks in. The `agent:` namespace is reserved and
   will be refused.
3. **Optionally pick a baseline** to run after the rebuild. This is what turns
   a bare install back into a working server.
4. **Confirm with all three factors:** your password, your authenticator code,
   and the hostname typed exactly. The submit button stays disabled until the
   hostname matches.
5. **A 60-second abort window opens.** Nothing has been touched yet. Aborting
   here — or any time before the reboot — removes the staged files and leaves
   the machine exactly as it was.

After that the machine stages the installer, writes a **one-shot** boot entry,
and reboots. One-shot matters: if the installer fails to come up, the entry
lapses and the machine returns to its old OS unaided.

Its alerts are suppressed for the length of the job, so a rebuild does not
page everyone. The host is still shown as offline, because it is.

## Reading the job

| State | What is happening | Can you stop it? |
|---|---|---|
| `pending` | 60-second abort window | Yes |
| `staging` | Downloading and verifying kernel + initrd | Yes |
| `staged` | Ready to reboot; nothing altered yet | Yes |
| `rebooting` | One-shot entry written, machine rebooting | No |
| `installing` | Installer confirmed running; **disk being wiped** | No |
| `enrolling` | Installed; waiting for the agent's first check-in | No |
| `completed` | Tag applied, baseline dispatched | — |
| `failed` / `aborted` / `timed_out` | Reason recorded on the job | — |

`failed` before the reboot means nothing was touched. `timed_out` during
`installing` or `enrolling` means the machine needs hands.

## When it goes wrong

| Symptom | Likely cause | What to do |
|---|---|---|
| Preflight refuses | Unknown bootloader, `/boot` too small, wrong arch, missing disk | Read the failure list; nothing was touched |
| `failed` during staging | Checksum mismatch or download failure | Re-check the image; the bootloader was never modified |
| Old OS comes back, job `failed` | Installer never booted | The one-shot entry did its job. Check kernel/initrd match the machine's firmware mode |
| `timed_out` in `installing` | Install died part-way | **Physical access required** |
| `timed_out` in `enrolling` | Installed, but the agent cannot reach Vigil | Almost always a static-IP profile that does not match the machine's network |

---

## Pre-release smoke test

**No CI job can test an actual wipe.** Everything above the boot boundary has
automated coverage; the boot-and-install itself does not, and cannot. This
procedure is the only coverage that half gets, and it runs before tagging a
release.

Run it once per family — Ubuntu, Debian, RHEL — against a throwaway VM.

- [ ] VM created, agent enrolled, `allow_reprovision: true` set and agent
      restarted.
- [ ] Preflight returns `ok: true` and lists the expected disks with correct
      sizes and mount points.
- [ ] Ceremony refuses a wrong typed hostname, a wrong password, and a wrong
      authenticator code — each tried once.
- [ ] Rebuild accepted; **abort deliberately during the 60-second window**.
      Confirm the machine stays up, keeps checking in, and
      `/boot/vigil-reprovision/` is gone.
- [ ] Re-run and let it proceed. Job reaches `installing`, which confirms the
      installer booted and fetched its answer file.
- [ ] **Machine installs with no interactive prompt.** This is the step golden
      files cannot cover — a renderer regression shows up here as an installer
      sitting at a question.
- [ ] Agent re-enrols. Host reappears with the **same UUID**, a rotated agent
      token, and cleared inventory.
- [ ] Completion tag applied; chosen baseline runs.
- [ ] Old agent token no longer authenticates.
- [ ] Point a profile at a nonexistent disk; confirm preflight refuses before
      anything is staged.

Record the result per family in the release notes. If any step fails, the
release does not ship.
