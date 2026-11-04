"""Seed a database the way an existing install looks, for the upgrade test.

Run with the PREVIOUS release's code (``manage.py shell < upgrade-seed.py``),
after its migrations. Only models and fields stable across releases are used,
so the same file seeds any recent release. scripts/upgrade-verify.py then
checks, with the new code after ``migrate``, that every row came through.
"""
from django.contrib.auth import get_user_model

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.playbooks.models import Playbook, PlaybookStep
from apps.policies.models import UpdatePolicy
from apps.tasks.models import TaskDefinition

User = get_user_model()
admin = User.objects.create_user("upgrade-admin", password="upgrade-pass-123")
UserProfile.objects.create(user=admin, role=Role.ADMIN)
viewer = User.objects.create_user("upgrade-viewer", password="upgrade-pass-123")
UserProfile.objects.create(user=viewer, role=Role.VIEWER)

hosts = []
for name in ("upg-web-1", "upg-web-2", "upg-db-1"):
    hosts.append(Host.objects.create(hostname=name, agent_token=f"upg-{name}",
                                     status=Host.Status.ONLINE, mode="managed",
                                     tags=["prod", name.split("-")[1]]))

rule = AlertRule.objects.create(name="upg cpu", metric="cpu_percent", operator="gt",
                                threshold=90, severity=AlertRule.Severity.WARNING)
Alert.objects.create(host=hosts[0], rule=rule, severity="warning", message="upgrade test alert")

definition = TaskDefinition.objects.create(
    name="upg restart", owner=admin, risk_level="low",
    yaml_source="name: upg restart\nrisk: low\nactions:\n  - action: service_restart\n    params:\n      name: nginx\n")
playbook = Playbook.objects.create(name="upg baseline", created_by=admin, target_tags=["web"])
PlaybookStep.objects.create(playbook=playbook, definition=definition, order=0)
UpdatePolicy.objects.create(name="upg nightly", target_tags=["prod"])

try:  # Business, when it is installed
    from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole
    west = Site.objects.create(name="Upgrade West", slug="upgrade-west")
    HostSiteAssignment.objects.create(host=hosts[0], site=west)
    UserSiteRole.objects.create(user=viewer, site=west, role=Role.ADMIN)
    print("seeded with Business sites")
except ImportError:
    print("seeded without Business")
print("seed done")
