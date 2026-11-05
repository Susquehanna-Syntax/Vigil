"""Seed the end-to-end suite's server (``manage.py shell < seed.py``).

An instance admin and, when Business is installed, a West-only site admin,
both with a known TOTP secret; an approved managed host per site and one
pending host; a low-risk task and a playbook that runs it. run.py drives the
browser against exactly these rows.
"""
from django.contrib.auth import get_user_model
from django.utils.timezone import now

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.playbooks.models import Playbook, PlaybookStep
from apps.tasks.models import TaskDefinition
from vigil import scoping

TOTP = "JBSWY3DPEHPK3PXP"      # test-only secret; run.py derives codes from it
PASSWORD = "e2e-pass-123"

with scoping.system(reason="e2e seed: building fixtures for every site"):
    User = get_user_model()

    def user(name, role, **kw):
        u = User.objects.create_user(name, password=PASSWORD, **kw)
        profile = UserProfile.objects.create(user=u, role=role)
        profile.totp_secret = TOTP
        profile.totp_confirmed_at = now()
        profile.save()
        return u

    admin = user("e2e-admin", Role.ADMIN)

    def host(name, **kw):
        defaults = {"status": Host.Status.ONLINE, "mode": "managed", "tags": ["e2e"]}
        defaults.update(kw)
        return Host.objects.create(hostname=name, agent_token=f"e2e-{name}", **defaults)

    west_host = host("e2e-west-1")
    lab_host = host("e2e-lab-1")
    host("e2e-pending-1", status=Host.Status.PENDING)

    from apps.tasks.spec import parse_and_validate
    source = ("name: e2e check nginx\nactions:\n  - id: c\n    type: check_service\n"
              "    params:\n      service_name: nginx\n")
    definition = TaskDefinition.objects.create(
        name="e2e check nginx", owner=admin, yaml_source=source, parsed_spec=parse_and_validate(source))
    playbook = Playbook.objects.create(name="e2e baseline", created_by=admin, target_tags=["e2e"])
    PlaybookStep.objects.create(playbook=playbook, definition=definition, order=0)
    # One global wave, no ladder: a rollout that names no group walks it.
    from apps.tasks.models import PatchWave
    PatchWave.objects.create(name="e2e canary", order=1, tags=["e2e"], validation_hours=0)

    try:
        from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole
    except ImportError:
        print("seeded without Business")
    else:
        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        HostSiteAssignment.objects.create(host=west_host, site=west)
        HostSiteAssignment.objects.create(host=lab_host, site=lab)
        dana = user("e2e-dana", Role.VIEWER)
        UserSiteRole.objects.create(user=dana, site=west, role=Role.ADMIN)
        print("seeded with Business sites")
print("e2e seed done")
