"""Hunt matches: agent-reported rows stored per host, served per run.

The agent posts its hunt steps' matches with the task result; the ingest
stores one `HuntMatch` row per match (caps, sanitising, action looked up
from the task's signed params), and `GET /api/v1/tasks/runs/<id>/hunt/`
answers the spec's results view: columns, every targeted host's state,
and the paged/filterable match list.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import HuntMatch, Task, TaskDefinition, TaskRun
from vigil.schema_check import assert_shape

from .api_schema import (
    TasksCommunityCardSerializer,
    TasksDeployResponseSerializer,
    TasksForkPlanSerializer,
    TasksForkResponseSerializer,
    TasksHuntResultsSerializer,
    TasksRunDetailSerializer,
    TasksRunHistorySerializer,
    TasksTaskHistorySerializer,
    TasksTaskSerializer,
)
from .test_cascade_fork import _fake_catalog

now = timezone.now

HUNT_PARAMS = {"steps": [{"id": "jars", "action": "hunt_file", "params": {}}]}
HUNT_SPEC = {
    "name": "Hunt jars",
    "risk": "low",
    "actions": [{"id": "jars", "type": "hunt_file", "params": {"name": "*.jar"}}],
}
PLAIN_PARAMS = {"steps": [{"id": "svc", "action": "check_service", "params": {}}]}
PLAIN_SPEC = {
    "name": "No hunt",
    "risk": "low",
    "actions": [{"id": "svc", "type": "check_service", "params": {}}],
}


def _host(n):
    return Host.objects.create(
        hostname=f"h{n}",
        agent_token=("t" * 31) + str(n),
        status=Host.Status.ONLINE,
        mode=Host.Mode.MANAGED,
    )


def _task(host, state=Task.State.DISPATCHED, params=HUNT_PARAMS, **kw):
    t = Task.objects.create(
        host=host,
        action="_script",
        params=params,
        nonce=("n" * 31) + str(Task.objects.count() % 10),
        ttl_seconds=300,
        state=state,
        **kw,
    )
    Task.objects.filter(pk=t.pk).update(
        dispatched_at=now() - timedelta(seconds=1), state=state
    )
    return t


def _hunt_matches(n, prefix="m"):
    return [
        {
            "evidence_type": "file",
            "path": f"/tmp/{prefix}{i}.jar",
            "size": i + 1,
            "version": "2.14.1" if i % 2 else None,
        }
        for i in range(n)
    ]


class HuntResultsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_login(self.user)
        self.host = _host(1)

    def _post(self, task, steps):
        resp = self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps(
                {
                    "task_id": str(task.id),
                    "state": "completed",
                    "output": "done",
                    "steps": steps,
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {'t' * 31}{task.host.hostname[1:]}",  # _host(n): ("t" * 31) + str(n)
        )
        assert resp.status_code == 200, resp.content
        task.refresh_from_db()
        return task

    def _hunt_step(self, matches, **extra):
        step = {
            "id": "jars",
            "status": "ok",
            "result": {},
            "hunt": {
                "matches": matches,
                "truncated": False,
                "duration": 0.5,
                "timed_out": False,
            },
        }
        step.update(extra)
        return step

    def test_47_responses_match_the_api_schema(self):
        """Each body this phase typed, against the serializer its schema names.

        Every endpoint with data present — an empty list checks no item
        shape, so each has at least one row behind it. The catalog views are
        not here: they read GitHub, which no test reaches (see
        ``test_cascade_fork``)."""
        # A finished task, reported through the agent's own endpoint: its body
        # is the refreshed task, which TasksTaskSerializer describes. The file's
        # _post helper hands back the DB row, so the response comes from here.
        reporting = _task(self.host, Task.State.EXECUTING)
        reported = self.client.post(
            "/api/v1/tasks/result/",
            data=json.dumps(
                {
                    "task_id": str(reporting.id),
                    "state": "completed",
                    "output": "done",
                    "steps": [
                        {"id": "jars", "status": "ok", "result": {"path": "/tmp/a.jar"}}
                    ],
                }
            ),
            content_type="application/json",
            # _host(n)'s token is ("t" * 31) + str(n).
            HTTP_AUTHORIZATION=f"Bearer {'t' * 31}{self.host.hostname[1:]}",
        )
        self.assertEqual(reported.status_code, 200, reported.content)
        assert_shape(self, reported.json(), TasksTaskSerializer)

        # A deploy through the real endpoint: TOTP-gated like every other.
        operator = get_user_model().objects.create_user("op47", password="pw")
        UserProfile.objects.create(user=operator, role=Role.ADMIN)
        definition = TaskDefinition.objects.create(
            owner=operator,
            name="Clear temp",
            yaml_source="yaml",
            parsed_spec={
                "name": "Clear temp",
                "risk": "low",
                "actions": [{"id": "svc", "type": "clear_temp_files", "params": {}}],
            },
        )
        operator_client = APIClient()
        operator_client.force_login(operator)
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            deployed = operator_client.post(
                f"/api/v1/tasks/definitions/{definition.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456"},
                content_type="application/json",
            )
        self.assertEqual(deployed.status_code, 201, deployed.content)
        assert_shape(self, deployed.json(), TasksDeployResponseSerializer)

        # The agent's report and the deploy both landed in the task feed.
        history = self.client.get("/api/v1/tasks/history/").json()
        assert_shape(self, history, TasksTaskHistorySerializer)
        self.assertTrue(history["results"], "a task list needs an item to check")

        runs = self.client.get("/api/v1/tasks/runs/").json()
        assert_shape(self, runs, TasksRunHistorySerializer)
        self.assertTrue(runs["results"], "a run list needs an item to check")

        detail = self.client.get(f"/api/v1/tasks/runs/{deployed.json()['id']}/").json()
        assert_shape(self, detail, TasksRunDetailSerializer)
        self.assertTrue(detail["tasks"], "a run's task list needs an item")
        self.assertEqual(
            sorted(detail["summary"]), ["hosts", "outcomes", "per_host", "states"]
        )

    def test_47_community_catalog_matches_the_api_schema(self):
        """The three catalog views against a stubbed repo: no test reaches GitHub.

        The fork's ``created`` needs an item, and forking what you already hold
        is the clean no-op that leaves it empty — so the playbook goes first,
        while its tasks are still only in the catalog."""
        # The catalog view and the planner each look the fetcher up in their own
        # module, so both are patched (as in test_cascade_fork).
        for target in (
            "apps.tasks.views.catalog._fetch_community_templates",
            "apps.tasks.views._catalog._fetch_community_templates",
        ):
            patcher = patch(target, side_effect=_fake_catalog)
            patcher.start()
            self.addCleanup(patcher.stop)
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

        # The authoring gate is the role, not superuserdom: this user has no
        # profile row, and an ADMIN profile carries the legacy operator powers.
        author = get_user_model().objects.create_user("cat47", password="pw")
        UserProfile.objects.create(user=author, role=Role.ADMIN)
        self.client.force_login(author)

        cards = self.client.get("/api/v1/tasks/community/playbooks/").json()
        assert_shape(self, cards, TasksCommunityCardSerializer(many=True))
        self.assertTrue(cards, "a catalog list needs an item to check")
        card = next(c for c in cards if c["kind"] == "playbooks")
        self.assertTrue(card["requires"], "a playbook card lists what a fork pulls in")

        plan = self.client.get(
            "/api/v1/tasks/community/playbooks/standard-build.yaml/plan/"
        ).json()
        assert_shape(self, plan, TasksForkPlanSerializer)
        self.assertTrue(plan["needs"], "a plan needs an item to check")
        self.assertFalse(plan["have"])

        forked = self.client.post(
            "/api/v1/tasks/community/playbooks/standard-build.yaml/fork/"
        )
        self.assertEqual(forked.status_code, 201, forked.content)
        assert_shape(self, forked.json(), TasksForkResponseSerializer)
        self.assertTrue(forked.json()["created"], "a fork lists what it created")

        # Re-forking is the no-op: same body, ``already`` set, nothing created.
        again = self.client.post(
            "/api/v1/tasks/community/playbooks/standard-build.yaml/fork/"
        )
        self.assertEqual(again.status_code, 200, again.content)
        assert_shape(self, again.json(), TasksForkResponseSerializer)

    def test_47_hunt_results_match_the_api_schema(self):
        definition = TaskDefinition.objects.create(
            owner=self.user,
            name="Hunt jars",
            yaml_source="yaml",
            parsed_spec=HUNT_SPEC,
        )
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars",
            host_count=1,
            step_count=1,
            state=TaskRun.State.RUNNING,
            definition=definition,
            requested_by=self.user,
        )
        t1 = _task(self.host, Task.State.EXECUTING, run=run, step_order=0)
        self._post(t1, [self._hunt_step(_hunt_matches(2))])

        hunt_body = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/").json()
        assert_shape(self, hunt_body, TasksHuntResultsSerializer)
        self.assertTrue(hunt_body["hosts"], "a host list needs an item to check")

    def test_matches_are_stored_per_host(self):
        task = _task(self.host)
        self._post(task, [self._hunt_step(_hunt_matches(3))])

        rows = list(task.hunt_matches.all().order_by("step_id", "data"))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.host == self.host for r in rows))
        self.assertTrue(all(r.action == "hunt_file" for r in rows))
        self.assertEqual(rows[0].evidence_type, "file")
        self.assertEqual(
            rows[0].data, {"path": "/tmp/m0.jar", "size": 1, "version": None}
        )
        self.assertEqual(
            task.result_data["hunts"]["jars"],
            {"truncated": False, "timed_out": False, "duration": 0.5},
        )
        self.assertEqual(task.result_data["steps"][0]["result"], {})

    def test_action_comes_from_the_signed_task(self):
        task = _task(self.host)
        # The report carries a spoofed action for the step; the stored
        # action must come from the task's own signed params instead.
        self._post(task, [self._hunt_step(_hunt_matches(1), action="hunt_process")])

        row = task.hunt_matches.get()
        self.assertEqual(row.action, "hunt_file")

        task = _task(self.host)
        # A step id the signed task does not contain is skipped even if the
        # agent claims an action for it: no rows, no hunts bookkeeping.
        self._post(
            task,
            [
                {
                    "id": "rogue2",
                    "status": "ok",
                    "result": {},
                    "action": "hunt_file",
                    "hunt": {
                        "matches": _hunt_matches(1, "c"),
                        "truncated": False,
                        "duration": 0.1,
                        "timed_out": False,
                    },
                }
            ],
        )
        self.assertEqual(task.hunt_matches.count(), 0)
        self.assertNotIn("rogue2", task.result_data.get("hunts", {}))

    def test_bad_values_are_dropped(self):
        task = _task(self.host)
        matches = [
            "not a dict",
            {"path": "/tmp/x.jar"},  # missing evidence_type
            {"evidence_type": "x" * 41, "path": "/tmp/y.jar"},
            {
                "evidence_type": "file",
                "path": "/tmp/ok.jar",
                "size": 3,
                "ratio": 1.5,
                "found": True,
                "note": None,
                "nested": {"a": 1},
                "badkey2": [1],
                "longkey" + "k" * 57: "v",
                "long": "z" * 2000,
            },
        ]
        self._post(task, [self._hunt_step(matches)])

        row = task.hunt_matches.get()
        self.assertEqual(row.evidence_type, "file")
        self.assertEqual(
            row.data,
            {
                "path": "/tmp/ok.jar",
                "size": 3,
                "ratio": 1.5,
                "found": True,
                "note": None,
                "long": "z" * 1000,
            },
        )

    def test_non_finite_floats_are_dropped(self):
        # NaN/Infinity cannot travel through the JSON body, so this calls
        # the ingest directly (the API path is covered above).
        from apps.tasks.views import _ingest_hunt_matches

        task = _task(self.host)
        _ingest_hunt_matches(
            task,
            [
                self._hunt_step(
                    [
                        {
                            "evidence_type": "file",
                            "path": "/tmp/a.jar",
                            "nan": float("nan"),
                            "inf": float("inf"),
                            "ok": 1.5,
                        },
                    ]
                )
            ],
        )
        row = task.hunt_matches.get()
        self.assertEqual(row.data, {"path": "/tmp/a.jar", "ok": 1.5})

    def test_caps_per_step_and_task(self):
        task = _task(self.host)
        task.params = {
            "steps": [
                {"id": "jars", "action": "hunt_file", "params": {}},
                {"id": "jars2", "action": "hunt_file", "params": {}},
            ]
        }
        task.save(update_fields=["params"])
        self._post(
            task,
            [
                self._hunt_step(_hunt_matches(6000)),
                self._hunt_step(_hunt_matches(16000, "n"), id="jars2"),
            ],
        )
        # Per-step cap: 6000 -> 5000, 16000 -> 5000. The task cap (20000)
        # is above this total, so both steps hit only their own cap — and
        # the server marks them truncated, overriding the agent's claim.
        self.assertEqual(task.hunt_matches.count(), 10000)
        self.assertEqual(task.hunt_matches.filter(step_id="jars").count(), 5000)
        self.assertEqual(task.hunt_matches.filter(step_id="jars2").count(), 5000)
        self.assertTrue(task.result_data["hunts"]["jars"]["truncated"])
        self.assertTrue(task.result_data["hunts"]["jars2"]["truncated"])

    def test_rereport_replaces(self):
        task = _task(self.host)
        self._post(task, [self._hunt_step(_hunt_matches(3))])
        # A completed task's result cannot be re-posted (state machine), so
        # reset it to executing to exercise the idempotent ingest path.
        task.state = Task.State.EXECUTING
        task.save(update_fields=["state"])
        self._post(task, [self._hunt_step(_hunt_matches(2, "x"))])

        rows = list(task.hunt_matches.all())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.data["path"].startswith("/tmp/x") for r in rows))

    def test_results_api_states_and_columns(self):
        definition = TaskDefinition.objects.create(
            owner=self.user,
            name="Hunt jars",
            yaml_source="yaml",
            parsed_spec=HUNT_SPEC,
        )
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars",
            host_count=3,
            step_count=1,
            state=TaskRun.State.RUNNING,
            definition=definition,
            requested_by=self.user,
        )
        h1, h2, h3, h4 = self.host, _host(2), _host(3), _host(4)
        t1 = _task(h1, Task.State.EXECUTING, run=run, step_order=0)
        _task(h2, Task.State.DISPATCHED, run=run, step_order=1)
        _task(h3, Task.State.FAILED, run=run, step_order=2)
        _task(h4, Task.State.COMPLETED, run=run, step_order=3)
        self._post(t1, [self._hunt_step(_hunt_matches(3))])
        t1.refresh_from_db()
        self.assertEqual(t1.state, Task.State.COMPLETED)

        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertEqual(data["columns"], ["path", "size", "version"])
        by_host = {h["host_id"]: h for h in data["hosts"]}
        self.assertEqual(len(by_host), 4)
        self.assertEqual(by_host[str(h1.id)]["state"], "matched")
        self.assertEqual(by_host[str(h1.id)]["match_count"], 3)
        self.assertEqual(by_host[str(h2.id)]["state"], "pending")
        self.assertEqual(by_host[str(h3.id)]["state"], "error")
        self.assertEqual(by_host[str(h4.id)]["state"], "not_matched")
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["limit"], 1000)
        self.assertEqual(data["offset"], 0)
        match = data["matches"][0]
        self.assertEqual(match["host_id"], str(h1.id))
        self.assertEqual(match["hostname"], "h1")
        self.assertEqual(match["action"], "hunt_file")
        self.assertEqual(match["evidence_type"], "file")
        self.assertIn("path", match)

    def test_results_api_filters_and_paging(self):
        definition = TaskDefinition.objects.create(
            owner=self.user,
            name="Hunt jars",
            yaml_source="yaml",
            parsed_spec=HUNT_SPEC,
        )
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars",
            host_count=2,
            step_count=1,
            state=TaskRun.State.RUNNING,
            definition=definition,
            requested_by=self.user,
        )
        h2 = _host(9)
        t1 = _task(self.host, Task.State.EXECUTING, run=run, step_order=0)
        t2 = _task(h2, Task.State.EXECUTING, run=run, step_order=1)
        self._post(t1, [self._hunt_step(_hunt_matches(3))])
        self._post(t2, [self._hunt_step(_hunt_matches(2, "z"))])

        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.json()["total"], 5)

        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/?limit=2&offset=1")
        data = resp.json()
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["matches"]), 2)
        self.assertEqual(data["limit"], 2)
        self.assertEqual(data["offset"], 1)
        self.assertEqual(data["columns"], ["path", "size", "version"])

        resp = self.client.get(
            f"/api/v1/tasks/runs/{run.id}/hunt/?host={h2.id}&limit=5000"
        )
        data = resp.json()
        self.assertEqual(data["total"], 2)
        self.assertTrue(all(m["host_id"] == str(h2.id) for m in data["matches"]))
        # hosts and columns are always for the whole run.
        self.assertEqual(len(data["hosts"]), 2)
        self.assertEqual(len(data["columns"]), 3)

        # ?step= filters matches the same way.
        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/?step=jars")
        self.assertEqual(resp.json()["total"], 5)
        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/?step=nope")
        self.assertEqual(resp.json()["total"], 0)

    def _run_with(self, *tasks_states):
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars",
            host_count=len(tasks_states),
            step_count=1,
            state=TaskRun.State.RUNNING,
            requested_by=self.user,
        )
        return run

    def test_host_badges_read_every_step(self):
        # result_data["hunts"] is keyed by step id; a truncated or timed-out
        # step must show on the host row.
        run = self._run_with(1)
        t1 = _task(self.host, Task.State.EXECUTING, run=run, step_order=0)
        step = self._hunt_step(_hunt_matches(1))
        step["hunt"].update(truncated=True, timed_out=True)
        self._post(t1, [step])
        host = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/").json()["hosts"][0]
        self.assertEqual((host["truncated"], host["timed_out"]), (True, True))

    def test_bad_host_filter_is_400(self):
        run = self._run_with(1)
        _task(self.host, Task.State.EXECUTING, run=run, step_order=0)
        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/?host=not-a-uuid")
        self.assertEqual(resp.status_code, 400)

    def test_rereport_with_no_matches_clears_rows(self):
        run = self._run_with(1)
        t1 = _task(self.host, Task.State.EXECUTING, run=run, step_order=0)
        self._post(t1, [self._hunt_step(_hunt_matches(2))])
        self.assertEqual(HuntMatch.objects.filter(task=t1).count(), 2)
        t1.state = Task.State.EXECUTING
        t1.save()
        self._post(t1, [self._hunt_step([])])
        self.assertEqual(HuntMatch.objects.filter(task=t1).count(), 0)

    def test_run_with_only_relevant_probes_is_a_hunt_run(self):
        run = self._run_with(1)
        params = {
            "steps": PLAIN_PARAMS["steps"],
            "relevant": {
                "op": "all",
                "items": [
                    {
                        "probe": {
                            "id": "relevant-1",
                            "type": "hunt_process",
                            "params": {"name": "cron"},
                        },
                        "risk": 0,
                    }
                ],
            },
        }
        _task(self.host, Task.State.COMPLETED, run=run, step_order=0, params=params)
        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.status_code, 200)

    def test_run_without_hunts_is_404(self):
        definition = TaskDefinition.objects.create(
            owner=self.user,
            name="No hunt",
            yaml_source="yaml",
            parsed_spec=PLAIN_SPEC,
        )
        run = TaskRun.objects.create(
            name_snapshot="No hunt",
            host_count=1,
            step_count=1,
            state=TaskRun.State.RUNNING,
            definition=definition,
            requested_by=self.user,
        )
        _task(
            self.host, Task.State.COMPLETED, run=run, step_order=0, params=PLAIN_PARAMS
        )

        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.status_code, 404)

        resp = self.client.get(
            "/api/v1/tasks/runs/00000000-0000-0000-0000-000000000000/hunt/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_gets_403(self):
        definition = TaskDefinition.objects.create(
            owner=self.user,
            name="Hunt jars",
            yaml_source="yaml",
            parsed_spec=HUNT_SPEC,
        )
        run = TaskRun.objects.create(
            name_snapshot="Hunt jars",
            host_count=1,
            step_count=1,
            state=TaskRun.State.RUNNING,
            definition=definition,
            requested_by=self.user,
        )
        self.client.logout()
        resp = self.client.get(f"/api/v1/tasks/runs/{run.id}/hunt/")
        self.assertEqual(resp.status_code, 403)
