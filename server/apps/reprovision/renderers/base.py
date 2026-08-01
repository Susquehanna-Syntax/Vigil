"""Renderer interface and the shared enrolment block.

Registered by os_family so a fourth family (Windows, 2026.7/2026.8) is a new
registration rather than a branch in shared code — see docs/reprovisioning.md
§12.
"""


def enrolment_script(base_url: str, enroll_token: str) -> str:
    """One shell command line that installs the agent and enrols it.

    Returned as a single `sh -c`-able string rather than a list of commands
    because two of the three families need it wrapped to run inside the
    *installed* system rather than the installer's own environment, and the
    env-var prefix only survives if a shell interprets it:

    - Ubuntu subiquity late-commands run in the live installer, with the new
      system mounted at /target, so an unwrapped command installs the agent
      into a ramdisk that is discarded at reboot.
    - Debian's in-target execs its argument directly, so `in-target FOO=1 sh`
      looks for a binary literally named `FOO=1`.
    - RHEL %post is already chrooted into the target, so it needs no wrapper.

    The one-time token is exchanged for a real agent token by install.sh; it
    grants exactly one thing, once: become this one existing host (§4.3).
    """
    return (
        f"curl -fsSL {base_url}/api/v1/agent/install.sh -o /tmp/vigil-install.sh && "
        f"VIGIL_SERVER_URL={base_url} VIGIL_ENROLL_TOKEN={enroll_token} "
        "sh /tmp/vigil-install.sh && "
        "rm -f /tmp/vigil-install.sh"
    )


def merged_packages(profile, *always: str) -> list[str]:
    """Profile packages plus the ones Vigil always needs, order-stable and
    deduplicated — a repeated name makes the rendered file look careless even
    where the installer tolerates it."""
    out: list[str] = []
    for pkg in [*always, *(profile.extra_packages or [])]:
        name = str(pkg).strip()
        if name and name not in out:
            out.append(name)
    return out


def password_hash(profile) -> str:
    """The admin password crypt hash, or '*' (no password login) when unset.

    Never plaintext: what reaches the answer file is already a hash, so an
    intercepted file yields something to crack rather than a credential.
    """
    from apps.hosts.crypto import decrypt_secret

    return decrypt_secret(profile.admin_password_encrypted) or "*"


class Renderer:
    """One answer-file format."""

    family: str = ""

    def render(self, job, base_url: str, enroll_token: str) -> dict[str, str]:
        """Return {filename: content}. Ubuntu emits two files; the others one."""
        raise NotImplementedError

    def kernel_cmdline(self, job, base_url: str, answer_token: str) -> str:
        """Kernel command line that points the installer at its answer file."""
        raise NotImplementedError

    @staticmethod
    def _append_raw(body: str, job) -> str:
        """Append the escape hatch verbatim (§6). Always last, so it can
        override anything the typed fields produced."""
        raw = (job.profile.raw_append or "").strip()
        return f"{body}{raw}\n" if raw else body
