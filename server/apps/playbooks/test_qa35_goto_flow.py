"""A playbook's flow gains a "go to" node: ``{"goto": "<step_id>", "max": 3}``.

This phase is the server half only — accept, validate, store, export and
import. Running one (creating the ``_goto`` task and resolving it in the
release walk) is QA-36, and drawing it in the editor is QA-37.
"""
from rest_framework.test import APIClient

from apps.playbooks.community_yaml import parse, to_yaml
from apps.playbooks.flow import (
    FlowError,
    branch_guards,
    build_flow,
    flow_sequence,
    tree_from,
)
from apps.playbooks.models import Playbook
from apps.playbooks.test_playbook_branches import (
    CHECK,
    INSTALL,
    PATCH,
    VERIFY,
    BranchTestBase,
)


class GotoFlowTests(BranchTestBase):
    def _cleanup_source(self):
        return ("name: Clean temp\ncleanup: true\nrisk: low\nactions:\n"
                "  - type: check_service\n    params:\n      service_name: nginx\n")

    def converge_tree(self):
        """check → branch (patch, then go to verify | install) → cleanup → verify."""
        check, patch, install, cleanup = (
            self.definition(s) for s in (CHECK, PATCH, INSTALL, self._cleanup_source()))
        return [
            {"definition_id": str(check.id), "id": "check"},
            {"if": "steps.check.status == 'ok'",
             "then": [{"definition_id": str(patch.id), "id": "patch"},
                      {"goto": "verify"}],
             "else": [{"definition_id": str(install.id), "id": "install"}]},
            {"definition_id": str(cleanup.id), "id": "cleanup"},
            {"definition_id": str(cleanup.id), "id": "verify"},
        ]

    def test_forward_goto_converges(self):
        pb = self.playbook(self.converge_tree(), name="Converge")
        branch = pb.flow[1]
        self.assertEqual(branch["then"][-1], {"id": "g1", "goto": "verify", "max": 3})
        self.assertEqual([s.step_id for s in pb.steps.order_by("order")],
                         ["check", "patch", "install", "cleanup", "verify"])

    def test_backward_goto_saves_with_max(self):
        check, patch = self.definition(CHECK), self.definition(PATCH)
        pb = self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"definition_id": str(patch.id), "id": "patch", "on_failure": "continue"},
            {"if": "steps.patch.status == 'failed'",
             "then": [{"goto": "check", "max": 5}]},
        ])
        self.assertEqual(pb.flow[2]["then"], [{"id": "g1", "goto": "check", "max": 5}])

    def test_goto_rules(self):
        check, patch, install = (self.definition(s) for s in (CHECK, PATCH, INSTALL))

        def step(d, i=None):
            return {"definition": d, "id": i}

        def refused(items, fragment):
            with self.assertRaises(FlowError) as ctx:
                build_flow(items)
            self.assertIn(fragment, str(ctx.exception))

        # Anything after a go to could never run.
        refused([step(check, "check"),
                 {"if": "steps.check.status == 'ok'",
                  "then": [{"goto": "check"}, step(patch, "late")]}],
                "must be the last item")
        # The target names a step of this playbook.
        refused([step(check, "check"), {"goto": "nowhere"}], "is not a step")
        # max is a whole number 1..10 — not 0, not 11, not True, not "3".
        for bad in (0, 11, True, "3"):
            with self.subTest(max=bad):
                refused([step(check, "check"), {"goto": "check", "max": bad}],
                        "whole number from 1 to 10")
        # A go to is goto/max only.
        refused([step(check, "check"), {"goto": "check", "extra": 1}], "goto/max only")
        # Not into a sibling branch.
        refused([step(check, "check"),
                 {"if": "steps.check.status == 'ok'",
                  "then": [{"goto": "install"}],
                  "else": [step(install, "install")]}],
                "not into another branch")
        # Nine of them. Each must end its lane, so each gets its own branch.
        refused([step(check, "check"),
                 *[{"if": "steps.check.status == 'ok'", "then": [{"goto": "check"}]}
                   for _ in range(9)],
                 step(patch, "last")],
                "at most 8")

    def test_goto_may_leave_nested_branches(self):
        check, patch, install = (self.definition(s) for s in (CHECK, PATCH, INSTALL))
        pb = self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"if": "steps.check.status == 'ok'", "then": [
                {"definition_id": str(patch.id), "id": "patch"},
                {"if": "steps.patch.status == 'ok'", "then": [
                    {"goto": "check"}],
                 "else": [{"definition_id": str(install.id), "id": "install"},
                          {"goto": "patch"}]},
            ]},
        ])
        nested = pb.flow[1]["then"][1]
        self.assertEqual(nested["then"], [{"id": "g1", "goto": "check", "max": 3}])
        self.assertEqual(nested["else"], [{"step": "install"},
                                          {"id": "g2", "goto": "patch", "max": 3}])
        guards = branch_guards(pb.flow)
        self.assertEqual(sorted(guards), ["@g1", "@g2", "install", "patch"])
        self.assertEqual(guards["@g1"][1], "b1.then.b2.then")
        self.assertEqual(guards["@g2"][1], "b1.then.b2.else")

    def test_guards_cover_gotos(self):
        pb = self.playbook(self.converge_tree(), name="Guards")
        guards = branch_guards(pb.flow)
        self.assertEqual(guards["@g1"], ("(steps.check.status == 'ok')", "b1.then"))
        check = self.definition(CHECK)
        top = self.playbook([{"definition_id": str(check.id), "id": "check"},
                             {"goto": "check"}], name="Top")
        self.assertEqual(branch_guards(top.flow), {})

    def test_flow_sequence_document_order(self):
        pb = self.playbook(self.converge_tree(), name="Sequence")
        self.assertEqual([n.get("step") or "@" + n["id"] for n in flow_sequence(pb.flow)],
                         ["check", "patch", "@g1", "install", "cleanup", "verify"])
        self.assertEqual(flow_sequence(None), [])
        self.assertEqual(flow_sequence([]), [])

    def test_tree_from_and_yaml_round_trip(self):
        pb = self.playbook(self.converge_tree(), name="Round Trip")
        self.assertEqual(tree_from(pb)[1]["then"][-1], {"goto": "verify", "max": 3})

        text = to_yaml(pb)
        self.assertIn("goto: verify", text)
        tree = parse(text)["tree"]
        self.assertIn({"goto": "verify", "max": 3}, tree[1]["then"])
        # The exported steps are in document order: the go to takes no step
        # entry, so every position after it is one lower than the tree's.
        self.assertEqual([s["task"] for s in parse(text)["steps"]],
                         ["check-nginx", "patch-nginx", "install-nginx",
                          "clean-temp", "clean-temp"])

        imported = text.replace("name: Round Trip", "name: Round Trip Two")
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post("/api/v1/playbooks/yaml/", {"yaml": imported}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        self.assertEqual(Playbook.objects.get(name="Round Trip Two").flow, pb.flow)

    def test_linear_playbook_still_has_no_flow(self):
        check, verify = self.definition(CHECK), self.definition(VERIFY)
        pb = self.playbook([str(check.id), str(verify.id)])
        self.assertIsNone(pb.flow)
