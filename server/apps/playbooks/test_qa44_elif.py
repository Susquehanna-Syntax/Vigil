"""Or-if exits on a playbook decision (QA-44).

An ``if`` node carries any number of ``elif`` arms between ``then`` and
``else``, checked in order: the first true condition wins and ``else`` runs
only when none is true. The engine needs no change — an or-if is a different
guard expression on the same steps.
"""
from rest_framework.test import APIClient

from apps.hosts.models import Host
from apps.playbooks.community_yaml import parse, to_yaml
from apps.playbooks.flow import FlowError, branch_guards, build_flow, flow_sequence
from apps.playbooks.models import Playbook
from apps.playbooks.test_playbook_branches import (
    CHECK,
    INSTALL,
    PATCH,
    VERIFY,
    BranchTestBase,
)
from apps.playbooks.views import _validate_and_set_steps
from apps.tasks.models import Task, TaskRun

# The decision splits on the hunt step's count: 0, 1, 2, or anything else.
COUNT = "steps.check.result.count"
ARM = {"a": "then", "b": "elif1", "c": "elif2", "d": "else"}


def if_expr(n: int) -> str:
    return f"{COUNT} == {n}"


class OrIfTestBase(BranchTestBase):
    def four_way(self, name="PB"):
        check, patch, install, verify = (self.definition(s) for s in (CHECK, PATCH, INSTALL, VERIFY))
        return self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"if": if_expr(0),
             "then": [{"definition_id": str(patch.id), "id": "a"}],
             "elif": [{"if": if_expr(1), "then": [{"definition_id": str(install.id), "id": "b"}]},
                      {"if": if_expr(2), "then": [{"definition_id": str(verify.id), "id": "c"}]}],
             "else": [{"definition_id": str(patch.id), "id": "d"}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ], name=name)

    def flow_of(self, items):
        return build_flow(items)[1]

    def reported(self, n):
        """The hunt step's result report — the ``count`` the exits split on."""
        return [("find", {"matched": n > 0, "count": n, "truncated": False})]


class OrIfRunTests(OrIfTestBase):
    def test_four_way_split_takes_the_first_true_arm(self):
        for n, taken in ((0, "a"), (1, "b"), (2, "c"), (5, "d")):
            with self.subTest(count=n):
                # A fresh playbook per run: a chain runs once per host, so
                # each of the four counts needs its own dispatch.
                self.host = Host.objects.create(
                    hostname=f"web-{n}", agent_token=f"tok-elif-{n}",
                    status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
                run = self.dispatch(self.four_way(f"Router {n}"))
                tasks = self.by_step(run)
                self.report(tasks["check"], results=self.reported(n))
                # The release walk stops at the arm it releases, so only the
                # arms before it have been judged skipped by then.
                earlier = {"a": [], "b": ["a"], "c": ["a", "b"], "d": ["a", "b", "c"]}[taken]
                for before in earlier:
                    self.assertEqual(self.states(run)[before], Task.State.SKIPPED, before)
                self.assertEqual(self.states(run)[taken], Task.State.PENDING)
                self.assertEqual(self.states(run)["verify"], Task.State.BLOCKED)
                self.report(tasks[taken])
                self.assertEqual(self.states(run)["verify"], Task.State.PENDING)
                # The walk reaches the arms after the taken one only once the
                # taken step's task finishes.
                tasks = self.by_step(run)
                for other in (x for x in ARM if x != taken):
                    tasks[other].refresh_from_db()
                    self.assertEqual(tasks[other].state, Task.State.SKIPPED, other)
                    self.assertEqual(
                        tasks[other].result_output,
                        f"[SKIPPED] branch b1.{ARM[other]} not taken", other)
                self.report(tasks["verify"])
                run.refresh_from_db()
                self.assertEqual(run.state, TaskRun.State.COMPLETED)

    def test_first_true_wins_when_two_hold(self):
        check, patch, install, verify = (self.definition(s) for s in (CHECK, PATCH, INSTALL, VERIFY))
        pb = self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"if": if_expr(0),
             "then": [{"definition_id": str(patch.id), "id": "a"}],
             "elif": [{"if": f"{COUNT} > 0",
                       "then": [{"definition_id": str(install.id), "id": "b"}]},
                      {"if": f"{COUNT} > 1",
                       "then": [{"definition_id": str(verify.id), "id": "c"}]}],
             "else": [{"definition_id": str(patch.id), "id": "d"}]},
        ], name="First wins")
        run = self.dispatch(pb)
        tasks = self.by_step(run)
        self.report(tasks["check"], results=self.reported(5))
        # The walk stops at the elif1 arm it released, so only the ``then``
        # arm has been judged skipped by then; the rest follow once it ends.
        self.assertEqual(self.states(run)["b"], Task.State.PENDING)
        self.assertEqual(self.states(run)["a"], Task.State.SKIPPED)
        self.report(tasks["b"])
        self.assertEqual(self.states(run), {"check": Task.State.COMPLETED,
                                            "a": Task.State.SKIPPED,
                                            "b": Task.State.COMPLETED,
                                            "c": Task.State.SKIPPED,
                                            "d": Task.State.SKIPPED})


class OrIfStoredShapeTests(OrIfTestBase):
    def test_guards_for_two_way_branches_are_unchanged(self):
        check, patch, install = self.definition(CHECK), self.definition(PATCH), self.definition(INSTALL)

        def step(definition, step_id):
            return {"definition": definition, "id": step_id}

        _steps, flow = build_flow([
            step(check, "check"),
            {"if": "steps.check.result.count > 0",
             "then": [step(patch, "a")], "else": [step(install, "d")]},
        ])
        guards = branch_guards(flow)
        self.assertEqual(guards["a"], ("(steps.check.result.count > 0)", "b1.then"))
        self.assertEqual(guards["d"], ("not (steps.check.result.count > 0)", "b1.else"))

        _steps, flow = build_flow([
            step(check, "check"),
            {"if": if_expr(0), "then": [step(patch, "a")],
             "elif": [{"if": if_expr(1), "then": [step(patch, "b")]},
                      {"if": if_expr(2), "then": [step(patch, "c")]}],
             "else": [step(patch, "d")]},
        ])
        guards = branch_guards(flow)
        self.assertEqual(guards["a"], (f"({if_expr(0)})", "b1.then"))
        self.assertEqual(guards["b"], (f"not ({if_expr(0)}) and ({if_expr(1)})", "b1.elif1"))
        self.assertEqual(guards["c"],
                         (f"not ({if_expr(0)}) and not ({if_expr(1)}) and ({if_expr(2)})",
                          "b1.elif2"))
        self.assertEqual(guards["d"],
                         (f"not ({if_expr(0)}) and not ({if_expr(1)}) and not ({if_expr(2)})",
                          "b1.else"))

    def test_goto_guards_carry_the_or_if_terms(self):
        check, patch = self.definition(CHECK), self.definition(PATCH)
        flow = self.flow_of([
            {"definition": check, "id": "check"},
            {"if": if_expr(0), "then": [{"definition": patch, "id": "a"}],
             "elif": [{"if": if_expr(1), "then": [{"goto": "check", "max": 2}]}]},
        ])
        self.assertEqual(branch_guards(flow)["@g1"],
                         (f"not ({if_expr(0)}) and ({if_expr(1)})", "b1.elif1"))

    def test_stored_flow_omits_empty_elif(self):
        check = self.definition(CHECK)
        two_way = self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"if": "steps.check.result.count > 0",
             "then": [{"definition_id": str(self.definition(PATCH).id), "id": "a"}],
             "else": [{"definition_id": str(self.definition(INSTALL).id), "id": "d"}]},
        ], name="Two way")
        self.assertEqual(
            two_way.flow[1],
            {"id": "b1", "if": "steps.check.result.count > 0",
             "then": [{"step": "a"}], "else": [{"step": "d"}]})

        four = self.four_way()
        self.assertEqual([arm["if"] for arm in four.flow[1]["elif"]], [if_expr(1), if_expr(2)])

    def test_flow_sequence_order(self):
        four = self.four_way()
        self.assertEqual([n.get("step") for n in flow_sequence(four.flow)],
                         ["check", "a", "b", "c", "d", "verify"])

    def test_tree_from_carries_elif(self):
        four = self.four_way()
        yaml = to_yaml(four)
        self.assertIn("elif:", yaml)
        again = parse(yaml)
        self.assertEqual([arm["if"] for arm in again["tree"][1]["elif"]],
                         [if_expr(1), if_expr(2)])


class OrIfValidationTests(OrIfTestBase):
    def setUp(self):
        super().setUp()
        self.check = self.definition(CHECK)
        self.patch = self.definition(PATCH)

    def refused(self, items, fragment):
        with self.assertRaises(FlowError) as ctx:
            build_flow(items)
        self.assertIn(fragment, str(ctx.exception))

    def step(self, step_id=None):
        return {"definition": self.patch, "id": step_id}

    def arm(self, n, step_id):
        return {"if": if_expr(n), "then": [self.step(step_id)]}

    def test_or_if_may_not_name_a_step_in_the_same_decision(self):
        self.refused(
            [{"definition": self.check, "id": "check"},
             {"if": if_expr(0), "then": [self.step("a")],
              "elif": [{"if": "steps.a.status == 'ok'", "then": [self.step("b")]}]}],
            "branch b1 or-if 1: steps.a is not an earlier playbook step")

    def test_empty_or_if_then_is_refused(self):
        self.refused(
            [{"definition": self.check, "id": "check"},
             {"if": if_expr(0), "then": [self.step("a")],
              "elif": [{"if": if_expr(1), "then": []}]}],
            "an or-if's 'then' must list at least one step")

    def test_unknown_key_in_an_or_if_is_refused(self):
        self.refused(
            [{"definition": self.check, "id": "check"},
             {"if": if_expr(0), "then": [self.step("a")],
              "elif": [{"if": if_expr(1), "then": [self.step("b")],
                        "else": [self.step("c")]}]}],
            "an or-if is if/then only, not ['else']")

    def test_more_than_eight_or_ifs_is_refused(self):
        self.refused(
            [{"definition": self.check, "id": "check"},
             {"if": if_expr(0), "then": [self.step("a")],
              "elif": [self.arm(n, f"e{n}") for n in range(1, 10)]}],
            "at most 8 or-if exits")

    def test_eight_or_ifs_are_accepted(self):
        flow = self.flow_of([
            {"definition": self.check, "id": "check"},
            {"if": if_expr(0), "then": [self.step("a")],
             "elif": [self.arm(n, f"e{n}") for n in range(1, 9)]},
        ])
        self.assertEqual(len(flow[1]["elif"]), 8)

    def test_or_if_lane_is_at_the_then_depth(self):
        """An or-if lane is as deep as ``then``: level 3 nests there too."""
        # Three levels of decision, the third one reached through an or-if.
        accept = {"if": if_expr(3), "then": [self.step("a1")]}
        accept = {"if": if_expr(1), "then": [self.step("a3")],
                  "elif": [{"if": if_expr(11), "then": [accept]}]}
        build_flow([{"definition": self.check, "id": "check"},
                    {"if": "steps.check.status == 'ok'", "then": [accept]}])

        refuse = {"if": if_expr(4), "then": [self.step("d1")]}
        refuse = {"if": if_expr(3), "then": [self.step("d3")],
                  "elif": [{"if": if_expr(13), "then": [refuse]}]}
        refuse = {"if": if_expr(2), "then": [self.step("d4")],
                  "elif": [{"if": if_expr(14), "then": [refuse]}]}
        refuse = {"if": if_expr(1), "then": [self.step("d5")],
                  "elif": [{"if": if_expr(11), "then": [refuse]}]}
        self.refused([{"definition": self.check, "id": "check"},
                      {"if": "steps.check.status == 'ok'", "then": [refuse]}],
                     "deeper than 3")

    def test_goto_from_an_elif_lane(self):
        build_flow([
            {"definition": self.check, "id": "check"},
            {"if": if_expr(0), "then": [self.step("a")],
             "elif": [{"if": if_expr(1), "then": [{"goto": "check", "max": 2}]}]},
        ])
        self.refused(
            [{"definition": self.check, "id": "check"},
             {"if": if_expr(0), "then": [self.step("a"), {"goto": "b"}],
              "elif": [{"if": if_expr(1), "then": [self.step("b")]}]}],
            "not into another branch")

    def test_goto_within_its_own_or_if_lane(self):
        build_flow([
            {"definition": self.check, "id": "check"},
            {"if": if_expr(0), "then": [self.step("a")],
             "elif": [{"if": if_expr(1),
                       "then": [self.step("b"), self.step("c"), {"goto": "b", "max": 2}]}]},
        ])
        # Into a decision nested inside the lane is still another branch.
        self.refused(
            [{"definition": self.check, "id": "check"},
             {"if": if_expr(0), "then": [self.step("a")],
              "elif": [{"if": if_expr(1), "then": [
                  {"if": if_expr(2), "then": [self.step("deep")]},
                  {"goto": "deep"}]}]}],
            "not into another branch")

    def test_api_load_rejects_a_malformed_elif(self):
        pb = Playbook.objects.create(name="Bad", created_by=self.user)
        for broken in ("not a list", [{"if": if_expr(1), "then": "not a list"}]):
            with self.subTest(broken=broken):
                err = _validate_and_set_steps(pb, [
                    {"definition_id": str(self.check.id), "id": "check"},
                    {"if": if_expr(0), "then": [{"definition_id": str(self.patch.id), "id": "a"}],
                     "elif": broken},
                ])
                self.assertEqual(err.status_code, 400)
                self.assertIn("elif must be a list of {if, then}", err.data["detail"])


class OrIfYamlTests(OrIfTestBase):
    ELIF = (
        "    elif:\n"
        "      - if: steps.check.result.count == 3010\n        then:\n"
        "          - task: install-nginx\n            id: reboot\n"
        "      - if: steps.check.result.count == 1618\n        then:\n"
        "          - task: verify-web\n            id: retry\n")

    YAML = (
        "name: Router or-if\nsteps:\n"
        "  - task: check-nginx\n    id: check\n"
        "  - if: steps.check.result.count == 0\n    then:\n"
        "      - task: patch-nginx\n        id: done\n"
        + ELIF +
        "    else:\n"
        "      - task: patch-nginx\n        id: escalate\n"
        "  - task: verify-web\n    id: verify\n")

    def setUp(self):
        super().setUp()
        for source in (CHECK, PATCH, INSTALL, VERIFY):
            self.definition(source)

    def test_yaml_round_trip_with_elif(self):
        # The second import names the playbook it replaces: the create path
        # refuses a name that is already taken.
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post("/api/v1/playbooks/yaml/", {"yaml": self.YAML}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        pb = Playbook.objects.get(name="Router or-if")
        self.assertEqual([arm["if"] for arm in pb.flow[1]["elif"]], [if_expr(3010), if_expr(1618)])
        self.assertEqual([s.step_id for s in pb.steps.order_by("order")],
                         ["check", "done", "reboot", "retry", "escalate", "verify"])
        exported = to_yaml(pb)
        self.assertIn("elif:", exported)
        again = parse(exported)
        self.assertEqual(again["tree"][1], {
            "if": if_expr(0), "then": [{"step": 1}],
            "elif": [{"if": if_expr(3010), "then": [{"step": 2}]},
                     {"if": if_expr(1618), "then": [{"step": 3}]}],
            "else": [{"step": 4}]})
        resp = client.post("/api/v1/playbooks/yaml/",
                           {"yaml": exported, "playbook_id": str(pb.id)}, format="json")
        self.assertIn(resp.status_code, (200, 201), resp.content)
        self.assertEqual(Playbook.objects.get(name="Router or-if").flow, pb.flow)

    def test_yaml_rejects_a_malformed_elif(self):
        from vigil.contentyaml import ContentYamlError

        for broken, message in (
                ("    elif: nope\n", "a branch's 'elif' must be a list of if/then."),
                ("    elif:\n      - then:\n          - task: patch-nginx\n",
                 "each 'elif' needs an 'if' and a 'then' that lists at least one step."),
                ("    elif:\n      - if: steps.check.result.count == 1\n        then: []\n",
                 "each 'elif' needs an 'if' and a 'then' that lists at least one step.")):
            with self.subTest(broken=broken), \
                    self.assertRaisesMessage(ContentYamlError, "playbook: " + message):
                parse(self.YAML.replace(self.ELIF, broken))

    def test_api_round_trip_with_elif(self):
        check, patch, install, verify = (self.defs[name] for name in
                                         ("Check nginx", "Patch nginx", "Install nginx",
                                          "Verify web"))
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post("/api/v1/playbooks/", {"name": "API or-if", "definition_ids": [
            {"definition_id": str(check.id), "id": "check"},
            {"if": if_expr(0),
             "then": [{"definition_id": str(patch.id), "id": "a"}],
             "elif": [{"if": if_expr(1), "then": [{"definition_id": str(install.id), "id": "b"}]},
                      {"if": if_expr(2), "then": [{"definition_id": str(verify.id), "id": "c"}]}],
             "else": [{"definition_id": str(patch.id), "id": "d"}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ]}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual([s["branch"] for s in body["steps"]],
                         ["", "b1.then", "b1.elif1", "b1.elif2", "b1.else", ""])
        self.assertEqual([arm["if"] for arm in body["flow"][1]["elif"]], [if_expr(1), if_expr(2)])
        self.assertIn("elif:", to_yaml(Playbook.objects.get(name="API or-if")))
