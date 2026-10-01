"""New installs allowlist the read-only actions (M7 phase 08): switching a host
to managed can hunt and inventory without hand-editing agent.yml, and nothing
that changes the host is allowed by default."""
from django.template.loader import render_to_string
from django.test import SimpleTestCase

READ_ONLY = ["app_inventory", "check_service", "container_logs", "hunt_content", "hunt_file", "hunt_package",
             "hunt_port", "hunt_process", "hunt_registry", "hunt_service"]
STATE_CHANGING = ["restart_service", "install_package", "app_install", "app_uninstall",
                  "execute_script", "run_command", "delete_path", "reboot"]


class InstallAllowlistTests(SimpleTestCase):
    def _block(self, template):
        text = render_to_string(template, {"base_url": "https://vigil.example"})
        start = text.index("allowlist:")
        lines = []
        for line in text[start:].splitlines()[1:]:
            if not line.startswith("  - "):
                break
            lines.append(line[4:].strip())
        return lines

    def test_both_installers_write_the_read_only_allowlist(self):
        for template in ("agent_install.sh", "agent_install.ps1"):
            with self.subTest(template=template):
                block = self._block(template)
                self.assertEqual(block, READ_ONLY)
                for action in STATE_CHANGING:
                    self.assertNotIn(action, block)
