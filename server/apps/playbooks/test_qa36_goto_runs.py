"""A playbook's "go to" runs (QA-36).

``create_chain`` turns every flow leaf into a task: a step's own task, and a
``_goto`` task for each go-to node. The release walk resolves a ``_goto`` on
the server — it is never handed to an agent — jumping forward through the
blocked steps to the target, or cloning the lane from the target's newest
instance back up to the go-to so it runs again, up to ``max`` times.
"""
from apps.playbooks.expansion import PlaybookExpandError, expand_actions
from apps.playbooks.flow import build_flow
from apps.playbooks.models import Playbook, PlaybookStep
from apps.playbooks.test_playbook_branches import (
    CHECK,
    INSTALL,
    PATCH,
    VERIFY,
    BranchTestBase,
)
from apps.tasks.models import Task

CLEANUP = ("name: Clean temp\ncleanup: true\nrisk: low\nactions:\n"
           "  - type: check_service\n    params:\n      service_name: nginx\n")


class GotoRunTests(BranchTestBase):
    def pending(self, run):
        released = [t for t in run.tasks.all() if t.state == Task.State.PENDING]
        self.assertEqual(len(released), 1,
                         [(t.step_ref, t.action, t.state) for t in run.tasks.all()])
        return released[0]

    def chain(self, run):
        return list(run.tasks.order_by("step_order"))

    def converge(self):
        """check → branch (patch, then go to verify | install) → cleanup → verify.

        The branch reads ``steps.check.status``, which is the task's own
        outcome, so the else lane is reached by failing ``check`` — which its
        ``on_failure: continue`` lets the chain recover from.
        """
        check, patch, install, cleanup = (
            self.definition(s) for s in (CHECK, PATCH, INSTALL, CLEANUP))
        return self.playbook([
            {"definition_id": str(check.id), "id": "check", "on_failure": "continue"},
            {"if": "steps.check.status == 'ok'",
             "then": [{"definition_id": str(patch.id), "id": "patch"},
                      {"goto": "verify"}],
             "else": [{"definition_id": str(install.id), "id": "install"}]},
            {"definition_id": str(cleanup.id), "id": "cleanup"},
            {"definition_id": str(cleanup.id), "id": "verify"},
        ], name="Converge")

    def retry_tree(self, max_jumps=2):
        check, patch, verify = (self.definition(s) for s in (CHECK, PATCH, VERIFY))
        return self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"definition_id": str(patch.id), "id": "patch", "on_failure": "continue"},
            # A failed task's step results say nothing that failed, so the
            # branch reads the service the patch was meant to start: no
            # longer active is the patch having failed.
            {"if": "steps.patch.result.active == False",
             "then": [{"goto": "patch", "max": max_jumps}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ], name="Retry")

    def patch_fails(self, task):
        """Report `patch` as a failed step, and so a failed task."""
        self.report(task, results=[("start", {"active": False, "state": "failed"})])

    def patch_works(self, task):
        self.report(task, results=[("start", {"active": True, "state": "active"})])

    def test_goto_tasks_are_created_in_flow_order(self):
        run = self.dispatch(self.converge())
        chain = self.chain(run)
        self.assertEqual([t.action for t in chain],
                         ["_script", "_script", "_goto", "_script", "_script", "_script"])
        self.assertEqual([t.step_order for t in chain], [0, 1, 2, 3, 4, 5])
        goto = chain[2]
        self.assertEqual(goto.guard, "(steps.check.status == 'ok')")
        self.assertEqual(goto.branch, "b1.then")
        self.assertEqual(goto.params["goto_id"], "g1")
        self.assertEqual(goto.params["goto"], "verify")
        self.assertEqual(goto.params["max"], 3)
        self.assertEqual(goto.step_ref, "")
        self.assertEqual(goto.state, Task.State.BLOCKED)

    def test_forward_goto_skips_to_target(self):
        run = self.dispatch(self.converge())
        self.report(self.pending(run), results=[("find", {"count": 1})])
        self.report(self.pending(run))
        states = {t.step_ref: t.state for t in self.chain(run)}
        self.assertEqual(states["install"], Task.State.SKIPPED)
        self.assertEqual(states["cleanup"], Task.State.SKIPPED)
        self.assertEqual(self.pending(run).step_ref, "verify")
        for t in self.chain(run):
            if t.action == "_goto":
                self.assertEqual(t.state, Task.State.COMPLETED)
                self.assertEqual(t.result_output, "[GOTO] jumped ahead to verify")
            if t.step_ref == "cleanup":
                self.assertEqual(t.result_output, "[SKIPPED] jumped over by go to verify")

    def test_forward_goto_not_taken_runs_else_and_rest(self):
        run = self.dispatch(self.converge())
        # A failed `check` (which its on_failure: continue lets the chain go
        # on from) means the then lane — patch and the go-to — is not taken.
        self.report(self.pending(run), state="failed")
        states = {t.step_ref: t.state for t in self.chain(run)}
        self.assertEqual(states["patch"], Task.State.SKIPPED)
        self.assertEqual(self.pending(run).step_ref, "install")
        goto = next(t for t in self.chain(run) if t.action == "_goto")
        self.assertEqual(goto.state, Task.State.SKIPPED)
        self.assertEqual(goto.result_output, "[SKIPPED] branch b1.then not taken")
        self.report(self.pending(run))
        self.assertEqual(self.pending(run).step_ref, "cleanup")
        self.report(self.pending(run))
        self.assertEqual(self.pending(run).step_ref, "verify")
        self.report(self.pending(run))
        run.refresh_from_db()
        self.assertEqual(run.state, run.State.COMPLETED)

    def test_backward_goto_retries_then_gives_up(self):
        run = self.dispatch(self.retry_tree())
        self.report(self.pending(run), results=[("find", {"count": 1})])

        for jump in (1, 2):
            self.assertEqual(self.pending(run).step_ref, "patch")
            self.patch_fails(self.pending(run))
            self.assertEqual(self.pending(run).step_ref, "patch",
                             f"jump {jump} should replay patch")

        self.patch_fails(self.pending(run))
        self.assertEqual(self.pending(run).step_ref, "verify")
        jumped = [t for t in self.chain(run) if t.action == "_goto"
                  and t.state == Task.State.COMPLETED]
        self.assertEqual(len(jumped), 3)
        self.assertEqual(jumped[-1].result_output,
                         "[GOTO] went back to patch 2 times already — going on")
        patches = [t for t in self.chain(run)
                   if t.action == "_script" and t.step_ref == "patch"]
        self.assertEqual(len(patches), 3)

    def test_backward_goto_stops_when_fixed(self):
        run = self.dispatch(self.retry_tree())
        self.report(self.pending(run), results=[("find", {"count": 1})])
        self.patch_fails(self.pending(run))
        self.assertEqual(self.pending(run).step_ref, "patch")
        self.patch_works(self.pending(run))
        self.assertEqual(self.pending(run).step_ref, "verify")
        chain = self.chain(run)
        goto = [t for t in chain if t.action == "_goto"]
        self.assertEqual(len(goto), 2)
        self.assertEqual(goto[0].state, Task.State.COMPLETED)
        self.assertEqual(goto[0].result_output, "[GOTO] jumped back to patch (1/2)")
        self.assertEqual(goto[1].state, Task.State.SKIPPED)
        self.assertEqual(goto[1].result_output, "[SKIPPED] branch b1.then not taken")
        clones = [t for t in chain if t.action == "_script" and t.step_ref == "patch"]
        self.assertEqual(len(clones), 2)
        self.assertEqual(clones[1].guard, clones[0].guard)
        self.assertEqual(clones[1].params, clones[0].params)

    def test_goto_never_reaches_the_agent(self):
        for build in (self.converge, self.retry_tree):
            run = self.dispatch(build())
            self.report(self.pending(run), results=[("find", {"count": 1})])
            for _ in range(10):
                for t in self.chain(run):
                    self.assertNotIn(t.action == "_goto" and t.state,
                                     [Task.State.PENDING, Task.State.DISPATCHED],
                                     f"a _goto reached the agent in {t.state}")
                if not [t for t in self.chain(run)
                        if t.state in (Task.State.BLOCKED, Task.State.PENDING)]:
                    break
                task = self.pending(run)
                if task.step_ref == "patch":
                    self.patch_fails(task)
                else:
                    self.report(task, results=[("find", {"count": 1})])

    def test_inline_playbook_with_goto_refused(self):
        check, patch = self.definition(CHECK), self.definition(PATCH)
        steps, flow = build_flow([
            {"definition": check, "id": "check"},
            {"if": "steps.check.status == 'ok'", "then": [{"goto": "check"}]},
            {"definition": patch, "id": "patch"},
        ])
        playbook = Playbook.objects.create(name="Go To PB", created_by=self.user, flow=flow)
        for i, st in enumerate(steps):
            PlaybookStep.objects.create(playbook=playbook, definition=st["definition"],
                                        order=i, step_id=st["step_id"], branch=st["branch"])
        with self.assertRaises(PlaybookExpandError) as ctx:
            expand_actions([{"type": "playbook", "params": {"name": "Go To PB"}}])
        self.assertIn("go to", str(ctx.exception))
        # The same playbook runs fine on its own.
        run = self.dispatch(playbook)
        self.assertEqual([t.action for t in run.tasks.order_by("step_order")],
                         ["_script", "_goto", "_script"])

    def test_linear_and_branch_chains_unchanged(self):
        check, patch, install, verify = (
            self.definition(s) for s in (CHECK, PATCH, INSTALL, VERIFY))
        pb = self.playbook([
            {"definition_id": str(check.id), "id": "check"},
            {"if": "steps.check.result.count > 0",
             "then": [{"definition_id": str(patch.id), "id": "patch"}],
             "else": [{"definition_id": str(install.id), "id": "install"}]},
            {"definition_id": str(verify.id), "id": "verify"},
        ])
        run = self.dispatch(pb)
        chain = self.chain(run)
        self.assertNotIn("_goto", [t.action for t in chain])
        self.assertEqual([t.step_order for t in chain], [0, 1, 2, 3])
        self.assertEqual([t.step_ref for t in chain],
                         ["check", "patch", "install", "verify"])
