from .base import Renderer, enrolment_script, merged_packages, password_hash


class UbuntuRenderer(Renderer):
    """subiquity autoinstall, delivered through the nocloud-net data source,
    which requires both user-data and meta-data to be present."""

    family = "ubuntu"

    def render(self, job, base_url: str, enroll_token: str) -> dict[str, str]:
        p = job.profile
        # late-commands run in the live installer, not the installed system.
        # curtin in-target chroots into /target so the agent lands on the
        # machine rather than in a ramdisk that is discarded at reboot.
        script = enrolment_script(base_url, enroll_token).replace('"', r'\"')
        late = f'    - curtin in-target --target=/target -- sh -c "{script}"'
        packages = "\n".join(
            f"    - {pkg}" for pkg in merged_packages(p, "openssh-server", "curl"))
        keys = "\n".join(
            f"      - {k.strip()}"
            for k in p.ssh_authorized_keys.splitlines() if k.strip())

        if p.network_mode == "static":
            network = (
                "  network:\n"
                "    version: 2\n"
                "    ethernets:\n"
                "      primary:\n"
                "        match:\n"
                "          name: en*\n"
                f"        addresses: [{p.static_address}]\n"
                f"        gateway4: {p.gateway}\n"
                "        nameservers:\n"
                f"          addresses: [{p.dns}]\n"
            )
        else:
            network = (
                "  network:\n"
                "    version: 2\n"
                "    ethernets:\n"
                "      primary:\n"
                "        match:\n"
                "          name: en*\n"
                "        dhcp4: true\n"
            )

        body = (
            "#cloud-config\n"
            "autoinstall:\n"
            "  version: 1\n"
            f"  locale: {p.locale}\n"
            f"  timezone: {p.timezone}\n"
            "  keyboard:\n"
            f"    layout: {p.keyboard}\n"
            f"{network}"
            "  storage:\n"
            "    layout:\n"
            f"      name: {p.partition_scheme}\n"
            "      match:\n"
            f"        path: {p.disk_target}\n"
            "  identity:\n"
            f"    hostname: {job.host.hostname}\n"
            f"    username: {p.admin_username}\n"
            f'    password: "{password_hash(p)}"\n'
            "  ssh:\n"
            "    install-server: true\n"
            "    authorized-keys:\n"
            f"{keys}\n"
            "  packages:\n"
            f"{packages}\n"
            "  late-commands:\n"
            f"{late}\n"
        )
        return {
            "user-data": self._append_raw(body, job),
            "meta-data": (f"instance-id: vigil-{job.id}\n"
                          f"local-hostname: {job.host.hostname}\n"),
        }

    def kernel_cmdline(self, job, base_url: str, answer_token: str) -> str:
        return (f"autoinstall ds=nocloud-net;"
                f"s={base_url}/reprovision/answer/{answer_token}/")
