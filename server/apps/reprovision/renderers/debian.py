from .base import Renderer, enrolment_script, merged_packages, password_hash


class DebianRenderer(Renderer):
    """debian-installer preseed.

    The partman confirmations below are not decoration: without them d-i
    stops and asks a human, which on a remote machine means a box that has
    wiped itself and then sits at a prompt forever.
    """

    family = "debian"

    def render(self, job, base_url: str, enroll_token: str) -> dict[str, str]:
        p = job.profile
        # in-target execs its argument directly, so `in-target FOO=1 sh ...`
        # would look for a binary literally named "FOO=1". The whole script
        # goes through one shell inside the target instead.
        script = enrolment_script(base_url, enroll_token).replace("'", r"'\''")
        late = f"in-target sh -c '{script}'"
        packages = " ".join(merged_packages(p, "openssh-server", "curl"))

        if p.network_mode == "static":
            address = p.static_address.split("/")[0]
            network = (
                "d-i netcfg/disable_autoconfig boolean true\n"
                f"d-i netcfg/get_ipaddress string {address}\n"
                f"d-i netcfg/get_gateway string {p.gateway}\n"
                f"d-i netcfg/get_nameservers string {p.dns}\n"
                "d-i netcfg/confirm_static boolean true\n"
            )
        else:
            network = "d-i netcfg/use_dhcp boolean true\n"

        body = (
            f"d-i debian-installer/locale string {p.locale}\n"
            f"d-i keyboard-configuration/xkb-keymap select {p.keyboard}\n"
            f"{network}"
            f"d-i netcfg/get_hostname string {job.host.hostname}\n"
            f"d-i time/zone string {p.timezone}\n"
            "d-i clock-setup/utc boolean true\n"
            f"d-i partman-auto/disk string {p.disk_target}\n"
            f"d-i partman-auto/method string {p.partition_scheme}\n"
            f"d-i partman/default_filesystem string {p.filesystem}\n"
            "d-i partman-lvm/device_remove_lvm boolean true\n"
            "d-i partman-md/device_remove_md boolean true\n"
            "d-i partman-lvm/confirm boolean true\n"
            "d-i partman-lvm/confirm_nooverwrite boolean true\n"
            "d-i partman-partitioning/confirm_write_new_label boolean true\n"
            "d-i partman/choose_partition select finish\n"
            "d-i partman/confirm boolean true\n"
            "d-i partman/confirm_nooverwrite boolean true\n"
            f"d-i passwd/username string {p.admin_username}\n"
            f"d-i passwd/user-password-crypted password {password_hash(p)}\n"
            "d-i passwd/root-login boolean false\n"
            f"d-i pkgsel/include string {packages}\n"
            "d-i pkgsel/upgrade select none\n"
            "d-i grub-installer/only_debian boolean true\n"
            f"d-i grub-installer/bootdev string {p.disk_target}\n"
            "d-i finish-install/reboot_in_progress note\n"
            f"d-i preseed/late_command string {late}\n"
        )
        return {"preseed.cfg": self._append_raw(body, job)}

    def kernel_cmdline(self, job, base_url: str, answer_token: str) -> str:
        return (f"auto=true priority=critical "
                f"url={base_url}/reprovision/answer/{answer_token}")
