"""After ``migrate`` on a database seeded by upgrade-seed.py, prove that
nothing was lost and that the new code reads the old rows the way the
upgrade notes promise. Run with the NEW code: ``manage.py shell < ...``.
Exits non-zero, with every problem listed, when anything is off."""
import sys

from django.contrib.auth import authenticate, get_user_model

from apps.alerts.models import Alert
from apps.hosts.models import Host, Tag
from apps.playbooks.models import Playbook
from apps.policies.models import UpdatePolicy
from apps.tasks.models import PatchWave, TaskDefinition

problems = []


def expect(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        problems.append(what)


User = get_user_model()
expect(User.objects.filter(username__startswith="upgrade-").count() == 2, "both users survive")
expect(authenticate(username="upgrade-admin", password="upgrade-pass-123") is not None,
       "an old password still signs in")
hosts = Host.objects.unscoped(reason="upgrade test").filter(hostname__startswith="upg-")
expect(hosts.count() == 3, "all three hosts survive")
web = hosts.filter(hostname="upg-web-1").first()
expect(web is not None and sorted(web.tags) == ["prod", "web"], "a host keeps its tags")
expect(Tag.objects.filter(key__in=["prod", "web", "db"]).count() == 3, "tag rows exist for every tag")
expect(not Tag.objects.filter(key__in=["prod", "web", "db"], site_id__isnull=False).exists(),
       "existing tags start global (S1 04h)")
expect(not PatchWave.objects.filter(site_id__isnull=False).exists(), "existing waves start global")
expect(Alert.objects.unscoped(reason="upgrade test").filter(message="upgrade test alert").count() == 1,
       "the alert survives")
definition = TaskDefinition.objects.filter(name="upg restart").first()
expect(definition is not None, "the task definition survives")
playbook = Playbook.objects.unscoped(reason="upgrade test").filter(name="upg baseline").first()
expect(playbook is not None and playbook.steps.count() == 1, "the playbook and its step survive")
if playbook is not None and hasattr(playbook, "tasks_auto_sync"):
    expect(playbook.tasks_auto_sync is True,
           "an existing playbook follows its tasks' edits (decision 16: existing references auto-sync)")
expect(UpdatePolicy.objects.unscoped(reason="upgrade test").filter(name="upg nightly").exists(),
       "the update policy survives")

try:
    from apps_business.sites.models import HostSiteAssignment, UserSiteRole
except ImportError:
    print("no Business: site checks skipped")
else:
    expect(HostSiteAssignment.objects.filter(host__hostname="upg-web-1",
                                             site__slug="upgrade-west").exists(),
           "a host keeps its site")
    expect(UserSiteRole.objects.filter(user__username="upgrade-viewer").exists(), "site roles survive")

if problems:
    print(f"{len(problems)} upgrade problem(s)")
    sys.exit(1)
print("upgrade verified")
