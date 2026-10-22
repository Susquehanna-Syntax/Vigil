"""An installer fetched through a relay tells the agent to use that relay.

Over the link the request arrives with the link's own Host header, so a script
rendered from request.get_host() would bake the dashboard's inside address
into VIGIL_SERVER — an address the machine that ran curl cannot reach.
"""

from django.test import TestCase

from apps.relays.models import Relay
from apps.relays.trust import link_token


class InstallScriptThroughARelayTests(TestCase):
    def setUp(self):
        self.relay = Relay.objects.create(
            name="edge", url="https://edge.example.com/t/acme",
            kind=Relay.Kind.REMOTE, enabled=True)
        # Free has switched to its one relay: with the built-in relay on too,
        # this one would be past the limit and the gate would refuse it.
        Relay.objects.filter(kind=Relay.Kind.BUILTIN).update(enabled=False)

    def _get(self, path, **extra):
        return self.client.get(path, HTTP_HOST="localhost", **extra)

    def _through_link(self, path):
        return self._get(path, HTTP_X_VIGIL_LINK=link_token(),
                         HTTP_X_VIGIL_RELAY=str(self.relay.id),
                         HTTP_X_VIGIL_AGENT_IP="198.51.100.9")

    def test_every_script_names_the_relay(self):
        for path in ("/agent/install.sh", "/agent/install.ps1",
                     "/agent/uninstall.sh", "/agent/uninstall.ps1"):
            with self.subTest(path=path):
                body = self._through_link(path).content.decode()
                self.assertIn("https://edge.example.com/t/acme", body)
                self.assertNotIn("http://localhost", body)

    def test_a_direct_fetch_still_names_the_request_host(self):
        Relay.objects.filter(kind=Relay.Kind.BUILTIN).update(enabled=True)
        body = self._get("/agent/install.sh").content.decode()
        self.assertIn('VIGIL_SERVER="http://localhost"', body)
