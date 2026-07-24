# Remote access — checking in from outside the LAN

Vigil agents are **outbound-only**: they open HTTPS connections *to* the server
and never listen for inbound traffic. So making Vigil work away from home is
entirely about exposing the **server** at an address reachable from wherever the
agents (and your browser) are — a public hostname or a VPN/overlay address. The
agent and dashboard don't change; you point them at the new URL.

There is exactly one Vigil-side knob plus one safety flag:

| Variable | What it does |
|---|---|
| `VIGIL_PUBLIC_URL` | The external URL Vigil is reached at, e.g. `https://vigil.example.com`. Its host is added to `ALLOWED_HOSTS` and its origin to `CSRF_TRUSTED_ORIGINS` automatically, so you set one variable instead of three. |
| `VIGIL_TRUST_PROXY` | `true` when a proxy/tunnel terminates TLS in front of Vigil. Django then trusts `X-Forwarded-Proto`/`X-Forwarded-Host`, so `request.is_secure()`, secure cookies, and the login redirect allow-list all see the real public scheme and host. |

> **Security note on `VIGIL_TRUST_PROXY`.** Only turn it on when the proxy or
> tunnel is the *only* route to Vigil and always rewrites the forwarded headers.
> The shipped `docker-compose.yml` still publishes `9200:8000` for LAN use, so
> if you rely on a tunnel exclusively, remove that `ports:` mapping (or firewall
> it) before enabling the flag — otherwise a client reaching the container
> directly could spoof `X-Forwarded-Proto`.

Also set `VIGIL_SECURE_COOKIES=true` once you're serving over HTTPS.

Point the agent at the same URL: in the agent's `agent.yml`, set the server URL
to your `VIGIL_PUBLIC_URL` (or re-run the install one-liner from that URL —
`install.sh` bakes in `window.location.origin`).

---

## Option A — Cloudflare Tunnel (public hostname, no open ports)

Best when you want a real `https://vigil.example.com` on a domain you control,
with TLS handled by Cloudflare and **no inbound ports** opened on your network.

1. In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com) →
   **Networks → Tunnels**, create a tunnel and copy its **token**.
2. Add a **public hostname** to the tunnel: e.g. `vigil.example.com` → service
   `HTTP` → `http://web:8000` (the compose service name and internal port).
3. In your `.env` next to `docker-compose.yml`:
   ```dotenv
   TUNNEL_TOKEN=eyJhIjoi...           # from step 1
   VIGIL_PUBLIC_URL=https://vigil.example.com
   VIGIL_TRUST_PROXY=true
   VIGIL_SECURE_COOKIES=true
   ```
4. Start the stack with the tunnel profile:
   ```bash
   docker compose --profile tunnel up -d
   ```
   The bundled `cloudflared` service connects out to Cloudflare; agents and your
   browser reach `https://vigil.example.com` from anywhere.
5. (Recommended) remove or firewall the `web` service's `9200:8000` port
   mapping so the tunnel is the only path in.

## Option B — Tailscale (private overlay, no public exposure)

Best when you only need *your own* devices (and agent hosts on your tailnet) to
reach Vigil — nothing is published to the public internet.

1. Install Tailscale on the Docker host and `tailscale up`. Note its tailnet
   name, e.g. `vigil-host.tailnet-1234.ts.net`.
2. Expose Vigil over Tailscale. Simplest is plain HTTP on the tailnet IP
   (port `9200`); for HTTPS use Tailscale Serve:
   ```bash
   tailscale serve --bg http://localhost:9200
   ```
   which gives you `https://vigil-host.tailnet-1234.ts.net` with a tailnet cert.
3. In `.env`:
   ```dotenv
   VIGIL_PUBLIC_URL=https://vigil-host.tailnet-1234.ts.net
   VIGIL_TRUST_PROXY=true          # Tailscale Serve terminates TLS and forwards HTTP
   VIGIL_SECURE_COOKIES=true
   ```
   (If you use plain HTTP on the tailnet IP instead, set
   `VIGIL_PUBLIC_URL=http://100.x.y.z:9200` and leave `VIGIL_TRUST_PROXY=false`.)
4. `docker compose up -d`. Install agents on tailnet hosts pointing at the
   `ts.net` URL.

## Option C — Any reverse proxy (nginx, Caddy, Traefik)

If you already run a reverse proxy that terminates TLS:

1. Proxy your hostname to `http://<docker-host>:9200`, forwarding the standard
   headers:
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:9200;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_set_header X-Forwarded-Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   }
   ```
   (Caddy sets `X-Forwarded-*` automatically.)
2. In `.env`:
   ```dotenv
   VIGIL_PUBLIC_URL=https://vigil.example.com
   VIGIL_TRUST_PROXY=true
   VIGIL_SECURE_COOKIES=true
   ```
3. `docker compose up -d`.

---

## Troubleshooting

- **`Bad Request (400)` / `DisallowedHost`** — `VIGIL_PUBLIC_URL` host isn't in
  `ALLOWED_HOSTS`. Confirm the URL exactly matches the hostname you browse to
  (no path, no trailing slash). You can still set `DJANGO_ALLOWED_HOSTS`
  manually for extra hosts.
- **`Origin checking failed` / `CSRF verification failed` on login/POST** — the
  browser origin isn't trusted. `VIGIL_PUBLIC_URL` should have added it; if you
  reach Vigil at additional origins, add them to
  `DJANGO_CSRF_TRUSTED_ORIGINS` (scheme included).
- **Login redirects to `http://` or cookies don't stick** — set
  `VIGIL_TRUST_PROXY=true` (so Django sees HTTPS) and `VIGIL_SECURE_COOKIES=true`.
- **Agent shows offline over the tunnel** — check the agent's server URL matches
  `VIGIL_PUBLIC_URL` and that the tunnel/proxy forwards to `web:8000`
  (Cloudflare) or `:9200` (host reverse proxy).
