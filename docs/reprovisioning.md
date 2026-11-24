# Reprovisioning

Reprovisioning rebuilds a machine's operating system from the Vigil console.
You choose an image and an install profile, confirm the rebuild, and the
machine wipes its disk, installs unattended, and reconnects to Vigil under the
same host record. You can give it a tag and run a baseline when it is back.

Use it to return a machine to a known-good state without walking to it. This
is useful after configuration drift, after a suspected compromise, or after a
ransomware clean-up.

Reprovisioning is available in the Free edition.

**Warning:** A rebuild erases the disk of the target machine. Check the
hostname and disk before you confirm. Read [Limitations](#limitations) before
you use this feature on a machine that may be compromised.

## What you need

- A Vigil server with administrator access, or an operator account with the
  `rebuild` permission on the host's site (see [Roles and permissions](rbac.md)).
- An agent on the target machine, installed with `allow_reprovision` enabled
  (see [Allow a machine to be rebuilt](#allow-a-machine-to-be-rebuilt)).
- An operating system image in the image library, or a URL Vigil can download
  one from.
- An install profile for the image.
- The machine's root password, or the password of the account that confirms
  the rebuild, and a time-based one-time password (TOTP) code from your
  authenticator app.

## Supported systems

| Family | Distributions | Installer |
|---|---|---|
| Ubuntu | Ubuntu | Autoinstall |
| Debian | Debian | Preseed |
| RHEL | Rocky, Alma, RHEL, Fedora | Kickstart |

Vigil supports these bootloaders on the target machine:

- GRUB2, in BIOS or UEFI mode
- systemd-boot

A machine with any other bootloader fails the pre-flight check. Windows is not
supported.

## Set up a rebuild

### 1. Allow a machine to be rebuilt

By default, an agent refuses rebuild actions. To allow a machine to be rebuilt,
set `allow_reprovision` in its `agent.yml` file:

```yaml
# Permit this machine to be remotely wiped and reinstalled.
# Not implied by full_control. Off by default, everywhere.
allow_reprovision: false
```

Set the value to `true` to allow rebuilds. The `full_control` mode does not
allow them on its own. The install one-liner can set this flag when you enrol
a new agent.

Rebuild actions ignore the agent's mode and task allowlist. The permission to
wipe a machine is set on that machine only.

### 2. Add an image

You can add an image in two ways. Only administrators can add images, because
an image decides what code runs as root on every machine you rebuild.

**Pull an image.** Vigil downloads the image itself. You can choose a distribution
from the built-in catalog, or enter a custom URL. Every pull requires a SHA-256
checksum.

**Upload an image.** Upload an ISO file directly. Vigil computes its SHA-256
checksum as it receives the file.

Vigil does not download from cloud metadata addresses, loopback addresses, or
link-local addresses. Private network addresses are allowed, so you can use an
internal mirror.

When Vigil imports an image, it:

1. Reads the ISO and extracts the kernel, the initrd, and the install tree.
2. Checks the checksum.
3. Deletes the ISO and keeps the checksum.

Because the ISO is deleted, you cannot check the image again later. Vigil checks
it once, at import.

If you pull an image that is already downloading, importing, or ready, Vigil
returns a conflict error that names the existing entry. If a pull failed, you
can retry it, and Vigil reuses the same entry.

Image management is not available through the per-site operator permissions.

### 3. Create an install profile

An install profile holds the settings that the installer uses. Open
**Reprovision > Install profiles** to create or edit one. You can also use the
API.

| Setting | What it does |
|---|---|
| Name | A label for the profile. |
| Image | The image the profile installs. |
| Disk target | The disk the installer wipes and installs to. |
| Partition scheme and filesystem | How the installer partitions and formats the disk. |
| Network mode | `dhcp`, or `static` with an address, gateway, and DNS servers. |
| Timezone, locale, keyboard | System regional settings. |
| Admin username and password | The first account. The password is stored as a hash. |
| SSH authorized keys | Public keys for the admin account. |
| Extra packages | Packages the installer adds. |
| Completion tags | Tags applied to the host when the rebuilt machine checks in. |
| Raw append | Text added to the end of the answer file, for settings the form does not cover. |
| Deadline minutes | How long the rebuild may take. The default is 60 minutes from the reboot. |

The profile must have an SSH key or a password hash. Otherwise nobody can log
in to the rebuilt machine. A static network mode must have both an address and
a gateway. The API rejects a profile that breaks either rule.

Completion tags use the form `key:value`. You cannot use the `agent:` prefix,
because Vigil uses it for tags that agents report.

Vigil reads completion tags when the machine checks in, so an edit to the
profile during a rebuild changes the tags that are applied.

To preview the answer file for a host, use
`POST /api/v1/reprovision/profiles/{id}/preview/`. Secrets are hidden in the
preview.

### 4. Run a pre-flight check

Before you rebuild, run a pre-flight check on the host. It changes nothing on
the machine. Use it to check whether a host is ready for a rebuild.

Vigil checks:

| Check | The check fails when |
|---|---|
| Bootloader | The bootloader is not GRUB2 (BIOS or UEFI) or systemd-boot. |
| `/boot` free space | `/boot` has less than the kernel and initrd size plus 20 percent. |
| RAM | The machine has less RAM than the installer needs for its family. |
| Architecture | The machine's architecture does not match the image's architecture. |
| Target disk | The disk named in the profile is not present. |
| Network | The profile uses a static address on a different subnet from the machine's current address. |

The disk check lists every disk with its size and current mount points. Check
this list before you confirm. Selecting the wrong disk is the most likely
mistake, and only a person reading the list can catch it.

If the network check fails, the machine may install correctly and then never
come back online. You can override the check for a planned change of address.
Vigil records the override in the audit log.

### 5. Start a rebuild

Open the host, choose **Rebuild**, and select an image and a profile. To
confirm, enter:

1. The account password.
2. A TOTP code from your authenticator app.
3. The host's hostname.

Vigil asks for your password and TOTP code each time you confirm, even if you
signed in recently.

The confirmation shows the image checksum and the target disk. Check both
before you confirm.

## What happens during a rebuild

A rebuild moves through these states.

| State | What it means | Next state |
|---|---|---|
| `PENDING` | You confirmed the rebuild. Vigil waits 60 seconds. | `STAGING` after 60 seconds, or `ABORTED` if you abort. |
| `STAGING` | The agent downloads the kernel and initrd and checks their checksums. | `STAGED`, or `FAILED`. You can also abort. |
| `STAGED` | The files are on the disk, the pre-flight check passed, and the bootloader is unchanged. | `REBOOTING` when the commit starts, or `ABORTED`. |
| `REBOOTING` | The agent sets a one-time boot entry and restarts the machine. | `INSTALLING` when the installer fetches its answer file. |
| `INSTALLING` | The installer is running and is wiping the disk. | `ENROLLING` when the new agent redeems its token. |
| `ENROLLING` | The new agent is registered and waits for its first check-in. | `COMPLETED` at check-in. |
| `COMPLETED` | The tags are applied, the maintenance window is cleared, and the baseline starts. | None. This is the final state. |
| `FAILED`, `ABORTED`, `TIMED_OUT` | The rebuild stopped. Vigil records the reason. | None. |

You can abort during `PENDING`, `STAGING`, or `STAGED`. When you abort, Vigil
removes the staged files from `/boot/vigil-reprovision/`. Nothing is changed on
the bootloader until the reboot. After the reboot starts, you cannot abort the
rebuild.

If a rebuild passes its deadline, Vigil marks it `TIMED_OUT`. The default
deadline is 60 minutes from the reboot. You can change it in the install
profile. A slow network may need more time for a RHEL install than for a
Debian netinstall.

While a rebuild runs, Vigil suppresses alerts for the host. When the rebuild
ends, the suppression is removed.

### The point of no return

The installer's first request for its answer file is the point of no return.
At that point the disk starts to be wiped, and you can no longer recover the
machine remotely.

Before that point, a failed boot returns the machine to its old operating
system. The boot entry is a one-time entry, so it expires on its own.

### After the rebuild

When the new agent checks in, Vigil:

1. Applies the profile's completion tags. Tags that you set before the rebuild
   stay in place.
2. Clears the host's maintenance window.
3. Marks the rebuild job `COMPLETED`.

Vigil also:

- Replaces the host's agent token. The old token stops working when the
  installer starts.
- Clears the host's package inventory, container list, and vulnerability
  findings. They describe the old system.
- Sets the host's `rebuilt_at` time.

To return a host to a known-good state, give the machine a completion tag that
an auto-enrol rule uses. When the machine checks in with that tag, the rule
runs its playbook. The run appears in the host's run history. Playbooks that
run on a rebuilt machine work the same way for a machine rebuilt by any other
route.

## Security

Vigil protects a rebuild in these ways:

- **Password and TOTP.** A rebuild needs your password, a TOTP code, and the
  hostname. Vigil checks them each time.
- **Answer file.** The answer file is available only while the rebuild is in
  the `REBOOTING` or `INSTALLING` state. The URL has 256 bits of randomness. The
  file is generated when it is requested and is never stored on disk. It
  returns `Cache-Control: no-store`, and Vigil does not log it in full.
- **Stored secrets.** The root password is stored as a hash. Other secrets in
  the profile are encrypted at rest.
- **Enrolment token.** The token works once, for one job. It can enrol only
  the host that the job names, and it expires when the job's deadline passes.
  Vigil stores it as a hash.
- **Repeat fetches.** The installer can fetch the answer file again. Vigil
  serves repeat fetches only to the IP address that made the first request, and
  it logs each fetch.
- **Audit log.** Vigil records each state change, the user, the source IP
  address, the authentication factors, the image, the profile, the target disk,
  and each answer-file fetch.

### Plain HTTP

If `VIGIL_PUBLIC_URL` uses plain HTTP, the answer file crosses your network
without encryption. Vigil allows this for home labs. Before you rebuild, an
administrator must accept the risk.

Accept the risk once for the whole instance under **Settings > Identity &
Security > Transport Security**. Vigil records who accepted it and when. You
can withdraw the acceptance at any time.

API clients can instead send `acknowledge_plaintext_transport: true` with each
rebuild request. Vigil refuses a rebuild over plain HTTP from a public address
unless one of these is set.

## API reference

```
GET    /api/v1/reprovision/catalog/               distributions Vigil can download
POST   /api/v1/reprovision/images/pull/           download an image (administrators only)
POST   /api/v1/reprovision/images/upload/         upload an ISO (administrators only)
GET    /api/v1/reprovision/images/                list the image library
POST   /api/v1/reprovision/images/                register an image without downloading it (administrators only)
GET    /api/v1/reprovision/images/{id}/           image details and import status
DELETE /api/v1/reprovision/images/{id}/           delete an image (administrators only)

GET    /api/v1/reprovision/profiles/              list profiles
POST   /api/v1/reprovision/profiles/              create a profile
GET|PATCH|DELETE /api/v1/reprovision/profiles/{id}/
POST   /api/v1/reprovision/profiles/{id}/preview/ preview the answer file, secrets hidden

POST   /api/v1/reprovision/preflight/             run the pre-flight check (host in the body)
GET    /api/v1/reprovision/jobs/                  list rebuild jobs for sites you can see
POST   /api/v1/reprovision/jobs/                  start a rebuild
GET    /api/v1/reprovision/jobs/{id}/             job details and timeline
POST   /api/v1/reprovision/jobs/{id}/abort/       abort a job in PENDING, STAGING, or STAGED

GET    /reprovision/answer/<opaque>               used by the installer, no login
GET    /reprovision/tree/<image_id>/...           used by the installer, no login
POST   /api/v1/reprovision/enroll                 redeem the one-time token, limited per IP
```

The installer uses the `answer` and `tree` routes without credentials. These
routes are outside `/api/v1`.

Start a rebuild with a request like this:

```json
{
  "host": "<uuid>", "image": "<uuid>", "profile": "<uuid>",
  "password": "...", "totp": "123456",
  "typed_hostname": "web-01",
  "acknowledge_plaintext_transport": true
}
```

`acknowledge_plaintext_transport` is optional when the instance-wide
acceptance is recorded.

## Limitations

- **Compromised hosts.** A rebuild does not guarantee that malware is removed.
  If an attacker has control of the machine's kernel, the attacker can stop the
  wipe and report that it succeeded. Use a rebuild to return a drifted machine
  to a known-good state or to clean up after ransomware. For a host you suspect
  is compromised, reinstall it from out-of-band media that you control, such as
  a bare-metal reinstall or a management controller (BMC).
- **Proof of the wipe.** To confirm a rebuild worked, check the host's
  `rebuilt_at` time and whether its inventory was cleared. Do not rely only on
  the agent's report.
- **Stuck installs.** If an install stops part way through, or the machine
  installs but never reconnects, the rebuild ends as `TIMED_OUT`. You must
  fix the machine in person.
- **One host at a time.** Each rebuild job covers one host. Vigil does not
  support bulk rebuilds.
- **Image checks.** Vigil checks images with SHA-256 only. It does not verify
  GPG signatures from distributions.
- **Images are global.** Every site shares the same image library and profile
  list.
- **PXE and BMC.** Vigil does not support PXE network boot or out-of-band
  management (Redfish or IPMI).
- **Windows.** Windows is not supported. Vigil does not accept Windows images.

## Testing

Each release is tested as follows:

- The answer files for each family are checked against stored expected output.
  A change to the output fails the tests.
- Every state change and timeout path is tested.
- Security checks cover the answer-file window, repeat fetches from other
  addresses, token reuse, permissions, and the confirmation factors.

No automated test can run a complete wipe. Before each release, someone runs
a manual rebuild in a virtual machine for each family.
