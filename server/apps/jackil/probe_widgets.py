"""The widget endpoints and the deep links, through Django's test client over
the real URLconf — the same view stack runserver mounts.

Not a test module: it asserts nothing that test_integration_widgets.py does
not already assert. It exists so the response bodies can be read with eyes,
once, against real rows and a real minted key. Run with the same environment
as the test suite:

    env -C server USE_SQLITE=1 DJANGO_SECRET_KEY=test VIGIL_SIGNING_KEY_SEED=… \
        DJANGO_ALLOWED_HOSTS=testserver .venv/bin/python -m apps.jackil.probe_widgets
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vigil.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.test.runner import DiscoverRunner
from rest_framework.test import APIClient

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.jackil.models import IntegrationKey
from apps.tasks.models import Task, TaskRun

CALLS = (
    ("/api/v1/jackil/integration/widgets/", None),
    ("/api/v1/jackil/integration/widgets/hosts_online/", None),
    ("/api/v1/jackil/integration/widgets/stat/", {"metric": "alerts_critical"}),
    ("/api/v1/jackil/integration/widgets/alert_list/", {"limit": "5"}),
    ("/api/v1/jackil/integration/widgets/recent_runs/", None),
    ("/api/v1/jackil/integration/widgets/offline_hosts/", None),
    ("/api/v1/jackil/integration/widgets/nonsense/", None),
    ("/api/v1/jackil/integration/widgets/stat/", {"metric": "cpu"}),
    ("/api/v1/jackil/integration/widgets/alert_list/", {"limit": "plenty"}),
)


def main() -> None:
    runner = DiscoverRunner(verbosity=0, interactive=False)
    runner.setup_test_environment()
    old_config = runner.setup_databases()
    try:
        _exercise()
    finally:
        runner.teardown_databases(old_config)


def _exercise() -> None:
    admin = get_user_model().objects.create_superuser(username="desk", email="d@x",
                                                      password="pw")
    _, raw = IntegrationKey.mint(name="Helpdesk", user=admin)
    web = Host.objects.create(hostname="web1", agent_token="t" * 32,
                              status=Host.Status.ONLINE)
    old = Host.objects.create(hostname="old1", agent_token="u" * 32,
                              status=Host.Status.OFFLINE)
    rule = AlertRule.objects.create(name="cpu", category="cpu", metric="cpu",
                                    operator="gt", threshold=90, severity="critical")
    Alert.objects.create(host=web, rule=rule, severity="critical", message="disk 96% full")
    run = TaskRun.objects.create(name_snapshot="Nightly run", host_count=2, state="failed")
    Task.objects.create(host=old, run=run, action="check_service", nonce="n", params={})

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    for url, params in CALLS:
        response = client.get(url, params or {})
        print(f"{response.status_code} {url} {params or ''}\n    {response.content.decode()}")

    client.credentials()
    print("no key:", client.get("/api/v1/jackil/integration/widgets/").status_code)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    print("POST with key:", client.post("/api/v1/jackil/integration/widgets/", {},
                                        format="json").status_code)
    print("search item url:",
          client.get("/api/v1/jackil/integration/search/", {"q": "web1"})
          .json()["results"][0]["url"])


if __name__ == "__main__":
    main()
