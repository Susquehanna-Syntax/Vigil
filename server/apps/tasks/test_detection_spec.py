"""M10: detection tasks — severity, cves and boost: on the ordinary task spec."""
from django.test import SimpleTestCase

from apps.tasks.dispatch import task_params
from apps.tasks.spec import SpecError, parse_and_validate

LOG4SHELL = """
name: log4shell-leftover-jar
severity: critical
cves: [CVE-2021-44228]
relevant:
  any:
    - hunt_file: {name: "log4j-core-*.jar", version_lt: "2.17.1", scope: targeted}
boost:
  - hunt_process: {cmdline: java}
actions:
  - id: remove
    type: execute_script
    params:
      shell: bash
      script: |
        find /opt -name "log4j-core-2.1[0-6]*.jar" -delete
"""


class DetectionSpecTests(SimpleTestCase):
    def test_the_example_parses(self):
        spec = parse_and_validate(LOG4SHELL)
        self.assertEqual(spec["severity"], "critical")
        self.assertEqual(spec["cves"], ["CVE-2021-44228"])
        self.assertEqual([b["probe"]["id"] for b in spec["boost"]], ["boost-1"])
        self.assertEqual(spec["boost"][0]["probe"]["type"], "hunt_process")
        params, _risk, _exp = task_params(spec)
        self.assertEqual(params["boost"], [{"id": "boost-1", "type": "hunt_process",
                                            "params": {"cmdline": "java"}}])
        self.assertIn("relevant", params)

    def test_plain_tasks_carry_neither(self):
        spec = parse_and_validate("name: t\nactions:\n  - type: app_inventory\n")
        self.assertEqual((spec["severity"], spec["boost"]), ("", []))
        self.assertNotIn("boost", task_params(spec)[0])

    def test_refusals(self):
        bad = {
            "severity": "severity: urgent\n",
            "boost not a list": "boost: {hunt_process: {cmdline: java}}\n",
            "empty boost": "boost: []\n",
            "boost not a hunt": "boost:\n  - run_command: {command: id}\n",
            "boost grouped": "boost:\n  - any:\n      - hunt_process: {cmdline: java}\n",
            "boost bad params": "boost:\n  - hunt_process: {nope: 1}\n",
            "too many": "boost:\n" + "  - hunt_process: {cmdline: java}\n" * 6,
        }
        for label, extra in bad.items():
            with self.subTest(label):
                with self.assertRaises(SpecError):
                    parse_and_validate("name: t\n" + extra + "actions:\n  - type: app_inventory\n")
