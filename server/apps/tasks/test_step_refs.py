"""A later step may read an earlier step's status and declared outputs.

The values only exist on the host at run time, so a reference to a step that
has not run yet, or to an output the action never produces, has to be caught
when the task is saved — otherwise it resolves to nothing and the step quietly
does the wrong thing on every host.
"""
from django.test import SimpleTestCase

from apps.tasks.spec import SpecError, parse_and_validate, resolve_inputs


def _yaml(command: str = "echo hi", when: str = "", svc_param: str = "nginx") -> str:
    when_line = f"    when: '{when}'\n" if when else ""
    return (
        "name: Step refs\n"
        "risk: high\n"
        "actions:\n"
        "  - id: svc\n"
        "    type: check_service\n"
        "    params:\n"
        f'      service_name: "{svc_param}"\n'
        "  - id: after\n"
        "    type: run_command\n"
        f"{when_line}"
        "    params:\n"
        f'      command: "{command}"\n'
    )


class StepRefTests(SimpleTestCase):
    def _error(self, yaml_src: str) -> str:
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(yaml_src)
        return str(ctx.exception)

    def test_result_ref_to_earlier_declared_output_is_accepted(self):
        parsed = parse_and_validate(_yaml("echo ${{ steps.svc.result.active }}"))
        self.assertEqual(len(parsed["actions"]), 2)

    def test_status_ref_is_accepted(self):
        parse_and_validate(_yaml("echo ${{ steps.svc.status }}"))

    def test_ref_to_later_step_is_rejected(self):
        err = self._error(_yaml(svc_param="${{ steps.after.status }}"))
        self.assertTrue("not an earlier step" in err, err)

    def test_ref_to_self_is_rejected(self):
        err = self._error(_yaml("echo ${{ steps.after.status }}"))
        self.assertTrue("not an earlier step" in err, err)

    def test_undeclared_output_is_rejected(self):
        err = self._error(_yaml("echo ${{ steps.svc.result.nope }}"))
        self.assertTrue("has no output 'nope'" in err, err)

    def test_malformed_step_ref_is_rejected(self):
        err = self._error(_yaml("echo ${{ steps.svc.results.active }}"))
        self.assertTrue("malformed step reference" in err, err)

    def test_when_can_read_an_earlier_step(self):
        parse_and_validate(_yaml(when="steps.svc.result.active == True"))
        parse_and_validate(_yaml(when='steps.svc.status == "ok"'))

    def test_when_rejects_undeclared_output(self):
        err = self._error(_yaml(when="steps.svc.result.nope == 1"))
        self.assertTrue("has no output 'nope'" in err, err)

    def test_when_rejects_bad_steps_shape(self):
        for expr in ("steps.svc.foo == 1", "steps.svc == 1", "steps == 1",
                     "steps.svc.result == 1"):
            err = self._error(_yaml(when=expr))
            self.assertTrue("steps" in err, f"{expr}: {err}")

    def test_when_rejects_a_later_step(self):
        err = self._error(_yaml(when='steps.after.status == "ok"'))
        self.assertTrue("not an earlier step" in err, err)

    def test_steps_refs_are_not_substituted(self):
        parsed = parse_and_validate(_yaml("echo ${{ steps.svc.result.active }}"))
        resolved = resolve_inputs(parsed, {})
        self.assertEqual(resolved["actions"][1]["params"]["command"],
                         "echo ${{ steps.svc.result.active }}")
