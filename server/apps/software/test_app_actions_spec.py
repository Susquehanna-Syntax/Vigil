"""The three app actions' spec contract: params, risk tier, declared outputs.

An ``app`` param is an inventory id — the ``package_id`` the Apps page shows —
so the ids that page can display have to parse, and the ones carrying shell
syntax or starting with an option must not. Everything here is server-side;
what the agent does with a value that reached it is the other half of the
contract (``agent/tests/test_app_actions_linux.py``).
"""

from django.test import SimpleTestCase

from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import SpecError, parse_and_validate, resolve_inputs

#: One action block, so every case below is a whole task definition rather
#: than a fragment a reader could not paste into the editor.
_TEMPLATE = """name: App action
risk: standard
inputs:
  - id: pkg
    label: Package
    type: text
actions:
  - id: act
    type: {action}
    params: {params}
"""


def _definition(action: str, params: str) -> str:
    return _TEMPLATE.format(action=action, params=params)


class AppActionsSpecTests(SimpleTestCase):
    def test_valid_app_tasks_parse(self):
        uninstall_params = (
            "{app: '{0158093D-F809-455B-9429-6D16A4B5D118}', source: registry}"
        )
        for action, params in (
            ("app_install", "{app: openssl}"),
            ("app_install", "{app: libc6:amd64, version: 2.43-2ubuntu2.4}"),
            ("app_upgrade", "{}"),
            ("app_upgrade", "{app: openssl, source: dpkg}"),
            ("app_uninstall", uninstall_params),
        ):
            with self.subTest(action=action, params=params):
                parsed = parse_and_validate(_definition(action, params))
                self.assertEqual(parsed["risk"], "standard")
                self.assertEqual(parsed["actions"][0]["type"], action)

    def test_the_three_actions_are_standard_risk(self):
        for name in ("app_install", "app_upgrade", "app_uninstall"):
            with self.subTest(action=name):
                self.assertEqual(ACTION_REGISTRY[name]["risk"], "standard")
        self.assertEqual(ACTION_REGISTRY["app_install"]["required"], ["app"])
        self.assertEqual(ACTION_REGISTRY["app_install"]["optional"],
                         ["source", "version"])
        self.assertEqual(ACTION_REGISTRY["app_upgrade"]["required"], [])
        self.assertEqual(ACTION_REGISTRY["app_upgrade"]["optional"],
                         ["app", "source"])
        self.assertEqual(ACTION_REGISTRY["app_uninstall"]["required"], ["app"])

    def test_outputs_are_referenceable_by_a_later_step(self):
        """`steps.x.result.installed_version` in a later `when:` is the point
        of declaring outputs — an undeclared field would be refused."""
        spec = """name: Install then report
risk: standard
actions:
  - id: inst
    type: app_install
    params: {app: openssl, source: dpkg}
  - id: up
    type: app_upgrade
    params: {}
    when: steps.inst.result.installed_version != "" and steps.inst.result.source == "dpkg"
  - id: rem
    type: app_uninstall
    params: {app: openssl}
    when: steps.up.result.upgraded >= 0 and steps.up.result.failed == 0
  - id: check
    type: app_inventory
    when: steps.rem.result.removed
"""
        try:
            parse_and_validate(spec)
        except SpecError as exc:
            self.fail(f"a step cannot reference an app action's outputs: {exc}")

    def test_unknown_output_reference_is_still_refused(self):
        # The guard the test above needs: a field app_install does not declare
        # must fail the same way, or "no error" proves nothing.
        spec = """name: Install then report
risk: standard
actions:
  - id: inst
    type: app_install
    params: {app: openssl}
  - id: report
    type: hunt_file
    when: steps.inst.result.nonsense == "x"
    params: {name: '*.log'}
"""
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(spec)
        self.assertIn("nonsense", str(ctx.exception))

    def test_unsafe_values_refused(self):
        bad = [
            ("app_install", "{app: 'foo;rm -rf /'}"),
            ("app_install", "{app: '-oProxy=x'}"),
            ("app_install", "{app: 'a b'}"),
            ("app_install", "{app: openssl, source: apt}"),
            ("app_install", "{app: openssl, version: '1.0.1 '}"),
            ("app_install", "{app: firefox, source: snap, version: '1.2'}"),
            ("app_install", "{app: firefox, source: flatpak, version: '1.2'}"),
            ("app_install", "{app: firefox, source: registry, version: '1.2'}"),
            ("app_upgrade", "{app: 'a|b'}"),
            ("app_uninstall", "{app: 'openssl$', source: dpkg}"),
        ]
        for action, params in bad:
            with self.subTest(action=action, params=params), \
                    self.assertRaises(SpecError):
                parse_and_validate(_definition(action, params))

    def test_version_pinning_refused_per_source_with_the_source_named(self):
        for source in ("snap", "flatpak", "registry"):
            with self.assertRaises(SpecError) as ctx:
                parse_and_validate(_definition(
                    "app_install",
                    f"{{app: firefox, source: {source}, version: '1.2'}}"))
            self.assertIn("version pinning is not supported",
                          str(ctx.exception))
            self.assertIn(source, str(ctx.exception))

    def test_upgrade_with_no_app_is_every_outdated_app_and_is_allowed(self):
        parsed = parse_and_validate(_definition("app_upgrade", "{}"))
        self.assertEqual(parsed["actions"][0]["type"], "app_upgrade")
        self.assertEqual(parsed["actions"][0]["params"], {})

    def test_inputs_are_not_prejudged(self):
        """A param resolved per deploy cannot be judged at parse time; the
        agent re-validates the value it ends up with."""
        for action in ("app_install", "app_upgrade", "app_uninstall"):
            with self.subTest(action=action):
                spec = _definition(action, "{app: '${{ inputs.pkg }}'}")
                parsed = parse_and_validate(spec)
                self.assertEqual(parsed["actions"][0]["params"]["app"],
                                 "${{ inputs.pkg }}")
                resolved = resolve_inputs(parsed, {"pkg": "openssl"})
                self.assertEqual(resolved["actions"][0]["params"]["app"],
                                 "openssl")

    def test_input_reference_in_source_and_version_is_not_prejudged(self):
        parsed = parse_and_validate(_definition(
            "app_install",
            "{app: '${{ inputs.pkg }}', source: '${{ inputs.pkg }}', "
            "version: '${{ inputs.pkg }}'}"))
        self.assertEqual(parsed["actions"][0]["params"]["source"],
                         "${{ inputs.pkg }}")
        self.assertEqual(parsed["actions"][0]["params"]["version"],
                         "${{ inputs.pkg }}")

    def test_required_app_still_required(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_definition("app_install", "{}"))
        self.assertIn("app", str(ctx.exception))
        with self.assertRaises(SpecError):
            parse_and_validate(_definition("app_uninstall", "{source: dpkg}"))


class RegistryKeyNameTests(SimpleTestCase):
    """A registry Uninstall key may contain spaces (the Windows VM's
    "Oracle VirtualBox Guest Additions"); only app_uninstall with source registry
    accepts one, and a backslash or control character is still refused."""

    def _task(self, app, action="app_uninstall", source="registry"):
        return ("name: t\nrisk: standard\nactions:\n  - id: a\n    type: %s\n"
                "    params:\n      app: \"%s\"\n      source: %s\n" % (action, app, source))

    def test_registry_key_with_spaces(self):
        from apps.tasks.spec import SpecError, parse_and_validate
        parse_and_validate(self._task("Oracle VirtualBox Guest Additions"))
        with self.assertRaises(SpecError):
            parse_and_validate(self._task("a\\\\b"))
        with self.assertRaises(SpecError):   # spaces stay refused for other sources
            parse_and_validate(self._task("Oracle VirtualBox", source="winget"))
