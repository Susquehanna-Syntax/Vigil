"""M11 05b: containers and stacks outside the host detail — the Inventory
columns, the dashboard widget, and the outdated-image alert's Update link."""
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert
from apps.alerts.tasks import _get_or_create_docker_rule

from .models import ContainerStack, DockerContainer, Host

JS = Path(__file__).resolve().parents[2] / "static" / "js"


class InventoryContainerColumnsTests(TestCase):
    def setUp(self):
        self.dock = Host.objects.create(hostname="dock", agent_token="t1")
        self.bare = Host.objects.create(hostname="bare", agent_token="t2")
        for name, state in (("web", "running"), ("db", "running"), ("old", "exited")):
            DockerContainer.objects.create(host=self.dock, container_id=name, name=name,
                                           state=state, stack="shop" if name != "old" else "")
        ContainerStack.objects.create(host=self.dock, project="shop", ownership="adopted")
        ContainerStack.objects.create(host=self.dock, project="media")
        Alert.objects.create(host=self.dock, rule=_get_or_create_docker_rule(),
                             severity="warning", message="outdated",
                             fix_context={"container_name": "web", "image": "nginx"})
        user = get_user_model().objects.create_user("viewer", password="x")
        UserProfile.objects.create(user=user, role=Role.VIEWER)
        self.client = APIClient()
        self.client.force_authenticate(user)

    def _rows(self):
        resp = self.client.get("/api/v1/hosts/inventory/")
        self.assertEqual(resp.status_code, 200)
        return {r["hostname"]: r for r in resp.json()["rows"]}

    def test_each_row_counts_its_containers_and_names_its_stacks(self):
        dock = self._rows()["dock"]
        self.assertEqual((dock["containers"], dock["containers_running"], dock["containers_outdated"]),
                         (3, 2, 1))
        self.assertEqual(dock["stacks"], [{"project": "media", "ownership": "external"},
                                          {"project": "shop", "ownership": "adopted"}])

    def test_a_host_without_containers_reports_zero(self):
        bare = self._rows()["bare"]
        self.assertEqual((bare["containers"], bare["containers_outdated"], bare["stacks"]),
                         (0, 0, []))

    def test_a_resolved_alert_is_not_counted(self):
        Alert.objects.update(state=Alert.State.RESOLVED)
        self.assertEqual(self._rows()["dock"]["containers_outdated"], 0)


class ContainerSurfacesSourceTests(SimpleTestCase):
    def _src(self, name):
        return (JS / name).read_text()

    def test_inventory_has_container_and_stack_columns(self):
        inv = self._src("vigil-inventory.js")
        self.assertIn("id: 'containers'", inv)
        self.assertIn("id: 'stacks'", inv)

    def test_the_widget_groups_by_stack_and_flags_outdated(self):
        widgets = self._src("vigil-widgets.js")
        self.assertIn("c.outdated ? 'peach'", widgets)
        self.assertIn("[c.stack, c.image]", widgets)

    def test_an_outdated_image_alert_opens_the_update_task(self):
        alerts = self._src("vigil-alerts.js")
        self.assertIn('data-alert-action="update-container"', alerts)
        self.assertIn("openUpdateContainer(alert.host, alert.fix_context.container_name)", alerts)
        self.assertNotIn("recreate_container", alerts)
