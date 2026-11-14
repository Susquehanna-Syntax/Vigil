"""The definition endpoints phase 46 typed: each body against the serializer its
``@extend_schema`` names, with data present so a list's item shape is checked."""
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from vigil.schema_check import assert_shape

from .api_schema import (
    TasksDefinitionErrorSerializer,
    TasksDefinitionListRowSerializer,
    TasksDefinitionSerializer,
    TasksEditorTemplatesSerializer,
    TasksNotFoundSerializer,
    TasksRefusalsSerializer,
    TasksTaskSerializer,
    TasksValidateResponseSerializer,
)
from .models import Task, TaskDefinition, TaskRun
from .test_refusals import CHECK

BASE = "/api/v1/tasks"


class DefinitionApiSchemaTests(TestCase):
    def setUp(self):
        self.operator = get_user_model().objects.create_user("op46", password="pw")
        UserProfile.objects.create(user=self.operator, role=Role.OPERATOR)
        self.api = APIClient()
        self.api.force_authenticate(self.operator)
        self.viewer = get_user_model().objects.create_user("view46", password="pw")
        UserProfile.objects.create(user=self.viewer, role=Role.VIEWER)
        # A monitor-mode agent refuses every action, so the refusal list has a row.
        self.host = Host.objects.create(hostname="db-46", agent_token="tok-db-46" * 4,
                                        status=Host.Status.ONLINE, mode="monitor",
                                        agent_allowlist=["check_service"])

    def ok(self, resp, serializer, status=200):
        self.assertEqual(resp.status_code, status, resp.content)
        assert_shape(self, resp.json(), serializer)
        return resp.json()

    def create(self):
        return self.ok(self.api.post(f"{BASE}/definitions/", {"yaml_source": CHECK}, format="json"),
                       TasksDefinitionSerializer, 201)

    def test_46_responses_match_the_api_schema(self):
        definition = self.create()
        did = definition["id"]
        self.ok(self.api.post(f"{BASE}/definitions/", {"yaml_source": "name: [unclosed"},
                              format="json"), TasksDefinitionErrorSerializer, 400)

        # The library list, with a run so last_run is filled in, then without.
        run = TaskRun.objects.create(definition_id=did, requested_by=self.operator)
        Task.objects.create(host=self.host, run=run, requested_by=self.operator,
                            action="check_service", params={"service_name": "cron"})
        rows = self.ok(self.api.get(f"{BASE}/definitions/"), TasksDefinitionListRowSerializer(many=True))
        self.assertIsNotNone(rows[0]["last_run"])
        TaskDefinition.objects.filter(pk=did).update(visibility=TaskDefinition.Visibility.COMMUNITY)
        self.assertTrue(self.ok(self.api.get(f"{BASE}/definitions/?scope=community"),
                                TasksDefinitionListRowSerializer(many=True)))

        self.ok(self.api.get(f"{BASE}/definitions/{did}/"), TasksDefinitionSerializer)
        self.ok(self.api.put(f"{BASE}/definitions/{did}/", {"yaml_source": CHECK}, format="json"),
                TasksDefinitionSerializer)
        self.ok(self.api.get(f"{BASE}/definitions/{uuid.uuid4()}/"), TasksNotFoundSerializer, 404)

        self.ok(self.api.post(f"{BASE}/definitions/validate/", {"yaml_source": CHECK}, format="json"),
                TasksValidateResponseSerializer)
        self.ok(self.api.post(f"{BASE}/definitions/{did}/fork/", {}, format="json"),
                TasksDefinitionSerializer, 201)

        refusals = self.ok(self.api.get(f"{BASE}/definitions/{did}/refusals/?host_ids={self.host.id}"),
                           TasksRefusalsSerializer)
        self.assertTrue(refusals["refusals"], "an empty list checks no row shape")

        self.ok(self.api.post(f"{BASE}/definitions/{did}/archive/", {}, format="json"),
                TasksDefinitionSerializer)
        self.ok(self.api.post(f"{BASE}/definitions/{did}/archive/", {"restore": True}, format="json"),
                TasksDefinitionSerializer)

        task = Task.objects.get(run=run)
        self.ok(self.api.get(f"{BASE}/{task.id}/"), TasksTaskSerializer)
        self.ok(self.api.get(f"{BASE}/{uuid.uuid4()}/"), TasksNotFoundSerializer, 404)

        templates = self.ok(self.api.get(f"{BASE}/editor-templates/"), TasksEditorTemplatesSerializer)
        self.assertTrue(templates["results"])
        self.assertIsInstance(self.api.get(f"{BASE}/actions/").json(), dict)

    def test_46_refusals_match_the_api_schema(self):
        did = self.create()["id"]
        viewer = APIClient()
        viewer.force_authenticate(self.viewer)
        # A role that may not write tasks is refused before anything is read.
        self.ok(viewer.post(f"{BASE}/definitions/{did}/fork/", {}, format="json"),
                TasksDefinitionErrorSerializer, 403)
        self.ok(viewer.put(f"{BASE}/definitions/{did}/", {"yaml_source": CHECK}, format="json"),
                TasksNotFoundSerializer, 404)
        TaskDefinition.objects.filter(pk=did).update(visibility=TaskDefinition.Visibility.COMMUNITY)
        self.ok(viewer.put(f"{BASE}/definitions/{did}/", {"yaml_source": CHECK}, format="json"),
                TasksDefinitionErrorSerializer, 403)
        self.ok(viewer.post(f"{BASE}/definitions/{did}/archive/", {}, format="json"),
                TasksDefinitionErrorSerializer, 403)
