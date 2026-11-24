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
# Decision 16 backfills auto-sync onto playbooks made before the field existed.
# A previous release that already has the field made this one with the normal
# default, so the verify script must not expect the backfill: say which here.
_had_auto_sync = any(f.name == "tasks_auto_sync" for f in Playbook._meta.get_fields())
playbook = Playbook.objects.create(
    name="upg baseline", created_by=admin, target_tags=["web"],
    description="seeded with tasks_auto_sync" if _had_auto_sync else "")
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
