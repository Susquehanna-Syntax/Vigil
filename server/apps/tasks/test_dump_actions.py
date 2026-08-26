"""manage.py dump_actions — the action reference generator.

The community repo's references/actions.json is generated from
ACTION_REGISTRY by this command, and the community validator loads it.
These tests pin the shape so the generator and the registry cannot drift:
every registered action appears exactly once, entries carry only the four
documented keys, the output is deterministic (the checked-in file must not
churn), and --output writes only to the path given.
"""
import json
import os
import tempfile
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from .spec import ACTION_REGISTRY


def _run_to_stdout():
    buf = StringIO()
    call_command("dump_actions", stdout=buf)
    return buf.getvalue()


class DumpActionsTests(TestCase):
    def test_dump_actions_covers_every_registered_action(self):
        payload = json.loads(_run_to_stdout())
        self.assertEqual(
            sorted(payload["actions"]), sorted(ACTION_REGISTRY),
            "dump_actions must emit exactly the registered actions")
        self.assertEqual(payload["vigil_version"], settings.VIGIL_VERSION)

    def test_dump_actions_entry_shape(self):
        payload = json.loads(_run_to_stdout())
        for name, entry in payload["actions"].items():
            self.assertEqual(
                sorted(entry), ["label", "optional", "required", "risk"],
                f"{name}: entries carry exactly label/risk/required/optional")
        spot = payload["actions"]["restart_service"]
        reg = ACTION_REGISTRY["restart_service"]
        self.assertEqual(spot["label"], reg["label"])
        self.assertEqual(spot["risk"], reg["risk"])
        self.assertEqual(spot["required"], reg["required"])
        self.assertEqual(spot["optional"], reg["optional"])

    def test_dump_actions_writes_to_output_path(self):
        with tempfile.TemporaryDirectory() as out_dir, \
                tempfile.TemporaryDirectory() as cwd_dir:
            out_path = os.path.join(out_dir, "actions.json")
            buf = StringIO()
            old_cwd = os.getcwd()
            os.chdir(cwd_dir)
            try:
                call_command("dump_actions", output=out_path, stdout=buf)
            finally:
                os.chdir(old_cwd)
            with open(out_path, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(
                sorted(payload["actions"]), sorted(ACTION_REGISTRY))
            # Nothing may be written anywhere outside the given path.
            self.assertEqual(os.listdir(cwd_dir), [])

    def test_dump_actions_is_deterministic(self):
        self.assertEqual(_run_to_stdout(), _run_to_stdout())
