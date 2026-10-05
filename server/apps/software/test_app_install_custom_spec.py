"""Server-side validation and registration of ``app_install_custom``.

The action runs an arbitrary installer, so what the server refuses to sign is
the whole of its safety envelope: an https URL, a 64-hex digest, a kind that
names the one command allowed to run the file, and — for ``exe`` only — a switch
list with no shell metacharacters in it. Every one of these is applied again by
the agent, because a switch list resolved from an input at deploy time is one
this file never saw.
"""

from django.test import SimpleTestCase

from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import SpecError, parse_and_validate

_SHA = "3a7f1b0c5d8e2f4a6b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c"


def _flow(path, sha=None, kind=None, args=None, app=None):
    """One action's params as an operator writes them in a flow mapping.

    The braces and the commas are added here rather than spelled into each
    case: a flow mapping cannot hold a ``${{ … }}`` reference or an unquoted
    ``?``, and the quoting either needs has nothing to do with the rule under
    test.
    """
    parts = [f"url: {path}", f"sha256: {_SHA if sha is None else sha}"]
    if kind is not None:
        parts.append(f"kind: {kind}")
    if args is not None:
        parts.append(f"args: {args}")
    if app is not None:
        parts.append(f"app: {app}")
    return "{" + ", ".join(parts) + "}"


_GOOD = _flow("https://example.com/tools/agent.msi")


def _definition(params):
    return ("name: t\ndescription: d\nrisk: standard\ninputs:\n"
            "  - id: args\n    label: Silent-install switches\n    type: text\n"
            "actions:\n"
            f"  - id: a\n    type: app_install_custom\n    params: {params}\n")


def _error(params):
    """The validation message for a task's params, or "" when it validates."""
    try:
        parse_and_validate(_definition(params))
    except SpecError as exc:
        return str(exc)
    return ""


class AppInstallCustomSpecTests(SimpleTestCase):
    def test_custom_install_parses_high_risk(self):
        parsed = parse_and_validate(_definition(_GOOD))
        self.assertEqual(parsed["risk"], "high")
        self.assertEqual(parsed["actions"][0]["type"], "app_install_custom")

        self.assertEqual(ACTION_REGISTRY["app_install_custom"]["risk"], "high")
        self.assertEqual(ACTION_REGISTRY["app_install_custom"]["required"],
                         ["url", "sha256"])
        self.assertEqual(ACTION_REGISTRY["app_install_custom"]["optional"],
                         ["args", "app", "kind"])
        self.assertEqual(ACTION_REGISTRY["app_install_custom"]["outputs"],
                         {"installed_version": "str", "sha256": "str"})

    def test_valid_forms(self):
        cases = [
            # The kind comes from the extension; an upper-case digest is the
            # same digest.
            _flow("https://example.com/a.deb", sha=_SHA.upper()),
            _flow("https://example.com/a.rpm", kind="rpm"),
            # A signed artifact URL keeps its extension in the path: the query
            # is cut before the extension is read.
            _flow("'https://example.com/a.deb?Signature=k&Expires=1'"),
            # args only ever means something to an exe.
            _flow("https://example.com/a.exe", args="/S"),
            _flow("https://example.com/a.exe", args="'/quiet /norestart'",
                  kind="exe"),
            _flow("https://example.com/a.msi", app="openssl"),
            # The switch list may be resolved per deploy; the agent re-applies
            # the same rule to whatever it arrives as.
            _flow("https://example.com/a.exe", kind="exe",
                  args="'${{ inputs.args }}'"),
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertEqual(_error(params), "")

    def test_custom_install_refusals(self):
        cases = [
            # Plain http: nothing an attacker cannot swap on the wire.
            _flow("http://example.com/a.msi"),
            # 63 hex characters — one short of a digest — and one that is 64
            # characters but not 64 hex.
            _flow("https://example.com/a.msi", sha=_SHA[:-1]),
            _flow("https://example.com/a.msi", sha=_SHA[:-1] + "z"),
            # A kind no command here knows how to install.
            _flow("https://example.com/a.zip", kind="zip"),
            # No extension to infer a kind from, and no kind named either.
            _flow("https://example.com/download/42"),
            # A named kind the file is not: the command that installs is chosen
            # by the kind, and it would be handed a file of another shape.
            _flow("https://example.com/a.deb", kind="exe"),
            # A shell metacharacter in the switch list.
            _flow("https://example.com/a.exe", args="'/S;rm -rf /'"),
            # args means nothing to the other three kinds.
            _flow("https://example.com/a.deb", args="-qq"),
            # The inventory id keeps the grammar the other app_* actions use.
            _flow("https://example.com/a.msi", app="'-oProxy=x'"),
            # Both required params.
            "{url: https://example.com/a.msi}",
            "{sha256: " + _SHA + "}",
        ]
        for params in cases:
            with self.subTest(params=params):
                self.assertIn("app_install_custom", _error(params))

    def test_refusal_messages_name_the_param(self):
        cases = [
            (_flow("http://example.com/a.msi"), "must start with https://"),
            (_flow("https://example.com/a.msi", sha=_SHA[:-1]),
             "must be 64 hexadecimal characters"),
            (_flow("https://example.com/a.zip", kind="zip"), "kind"),
            (_flow("https://example.com/download/42"),
             "say kind: msi, exe, deb or rpm"),
            (_flow("https://example.com/a.exe", args="'/S|x'"), "args"),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                message = _error(params)
                self.assertTrue(message, "the task validated, and should not have")
                self.assertIn(expected, message)

    def test_url_length_and_whitespace(self):
        self.assertIn("2000",
                      _error(_flow(f"https://example.com/{'a' * 2000}.msi")))
        self.assertIn("whitespace",
                      _error(_flow("'https://example.com/a b.msi'")))

    def test_outputs_are_referenceable_by_a_later_step(self):
        spec = """name: Install then confirm
description: "Install an artifact no package manager carries, then check the service it provides."
risk: standard
actions:
  - id: install
    type: app_install_custom
    params: {url: https://example.com/tools/agent.msi, sha256: 3a7f1b0c5d8e2f4a6b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c, app: openssl}
  - id: confirm
    type: check_service
    params: {service_name: nginx, expect: active}
    when: steps.install.result.sha256 != ""
"""
        try:
            parse_and_validate(spec)
        except SpecError as exc:
            self.fail(f"a step cannot reference app_install_custom's outputs: {exc}")
