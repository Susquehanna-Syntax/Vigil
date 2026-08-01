from .base import Renderer, enrolment_script, merged_packages, password_hash


class RhelRenderer(Renderer):
    """Kickstart, for Rocky / Alma / RHEL / Fedora."""

    family = "rhel"

    def render(self, job, base_url: str, enroll_token: str) -> dict[str, str]:
        p = job.profile
        # %post is already chrooted into the installed system, so the script
        # runs as-is — no in-target wrapper needed, unlike the other two.
        post = enrolment_script(base_url, enroll_token)
        packages = "\n".join(
            merged_packages(p, "@^minimal-environment", "openssh-server", "curl"))
        keys = "\n".join(
            f'sshkey --username={p.admin_username} "{k.strip()}"'
            for k in p.ssh_authorized_keys.splitlines() if k.strip())
        pw = password_hash(p)

        if p.network_mode == "static":
            address, _, prefix = p.static_address.partition("/")
            network = (
                f"network --bootproto=static --ip={address} "
                f"--netmask={_netmask(prefix)} --gateway={p.gateway} "
                f"--nameserver={p.dns} --hostname={job.host.hostname}\n"
            )
        else:
            network = (f"network --bootproto=dhcp "
                       f"--hostname={job.host.hostname}\n")

        body = (
            "text\n"
            f"lang {p.locale}\n"
            f"keyboard {p.keyboard}\n"
            f"timezone {p.timezone} --utc\n"
            f"{network}"
            f"rootpw --lock\n"
            f"user --name={p.admin_username} --groups=wheel "
            f"--iscrypted --password={pw}\n"
            f"{keys}\n"
            f"ignoredisk --only-use={p.disk_target}\n"
            f"clearpart --all --initlabel --drives={p.disk_target}\n"
            f"autopart --type={p.partition_scheme} --fstype={p.filesystem}\n"
            "bootloader --location=mbr\n"
            "firstboot --disabled\n"
            "reboot\n"
            "\n"
            "%packages\n"
            f"{packages}\n"
            "%end\n"
            "\n"
            "%post --log=/root/vigil-enrol.log\n"
            f"{post}\n"
            "%end\n"
        )
        return {"kickstart.cfg": self._append_raw(body, job)}

    def kernel_cmdline(self, job, base_url: str, answer_token: str) -> str:
        return (f"inst.ks={base_url}/reprovision/answer/{answer_token} "
                f"inst.repo={base_url}/reprovision/tree/{job.image.id}/")


def _netmask(prefix: str) -> str:
    """Dotted-quad netmask from a CIDR prefix. Kickstart wants the old form."""
    bits = int(prefix or 24)
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))
