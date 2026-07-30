"""Exercised by tests_agpl_only in a subprocess with apps_business stripped.

Every core path that reads site data runs here. If one of them reaches a
Business relation by name — say Host.objects.filter(site_assignment__...) —
it raises FieldError and this exits non-zero.
"""
import django

django.setup()

from django.conf import settings  # noqa: E402
from apps.baselines.models import Baseline  # noqa: E402
from apps.hosts.models import Host  # noqa: E402
from vigil import scoping  # noqa: E402

assert not [a for a in settings.INSTALLED_APPS if a.startswith("apps_business")], \
    "probe must run without the Business apps"

# The façade must degrade rather than raise. None of these touch the DB.
assert scoping.scope_of(Baseline(name="x")) is None
assert scoping.execution_allowed(Baseline(name="x")) is True
assert scoping.sites_for_hosts([]) == {}

# Querysets must still *resolve* — this is where a stray site_assignment__
# traversal in core would raise FieldError.
str(scoping.resources_for(Baseline, None).query)
str(Host.objects.exclude(status=Host.Status.REJECTED).query)

print("agpl-only probe OK")
