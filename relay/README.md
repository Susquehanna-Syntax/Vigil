# vigil-relay

The relay container (`susquehannasyntax/vigil-relay`). Agents point their
`server_url` at the relay and call the same paths they call on Vigil; the relay
holds no data and reaches the dashboard only over an **outbound** WebSocket
*uplink* that the dashboard's `vigil-link` process dials (`/relay/uplink`,
QA-54). Each agent request is pushed down that uplink as a JSON frame and the
dashboard's answer comes back. The dashboard needs no inbound port.

The relay answers `GET /api/v1/health/` itself, forwards only the agent paths it
allowlists (everything else is 404 — the dashboard UI is never reachable here),
and buffers offline `POST /api/v1/checkin` on disk until the uplink returns. It
never stores or logs request bodies or `Authorization` headers.

## Environment

| Variable | Meaning |
| --- | --- |
| `RELAY_TENANTS` | JSON object of `slug -> sha256(key)`, for multi-tenant hosting |
| `RELAY_KEY_SHA256` | single tenant, stored as `{"default": <sha256(key)>}` |
| `RELAY_PORT` | listen port (default `8443`) |
| `RELAY_QUEUE_DIR` | offline buffer directory (default `/var/lib/vigil-relay/queue`) |
| `RELAY_TLS_CERT` / `RELAY_TLS_KEY` | serve TLS itself; otherwise plain HTTP behind a proxy |
| `RELAY_TRUST_PROXY` | take the client IP from the first `X-Forwarded-For` entry |

With several tenants an agent's URL is `https://relay.example/t/<slug>/api/v1/checkin`;
the relay strips `/t/<slug>`. The key itself never crosses the wire — the link
authenticates with `Authorization: Bearer <key>` and the relay compares its
`sha256` against the configured hash.

## Compose

```yaml
services:
  relay:
    image: susquehannasyntax/vigil-relay:2026.15.1
    restart: unless-stopped
    ports: ["8443:8443"]
    environment:
      RELAY_KEY_SHA256: "<sha256 of the relay key shown in the Vigil dashboard>"
      RELAY_TLS_CERT: /etc/tls/relay.crt
      RELAY_TLS_KEY: /etc/tls/relay.key
    volumes:
      - relay-queue:/var/lib/vigil-relay
volumes:
  relay-queue: {}
```
