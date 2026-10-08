{% autoescape off %}#!/bin/bash
# Vigil Agent installer — {{ base_url }}
# Linux / macOS
# Usage: curl -fsSL {{ base_url }}/agent/install.sh | sudo bash
#   or:  curl -fsSL {{ base_url }}/agent/install.sh | sudo env VIGIL_TOKEN=<token> bash
set -e

VIGIL_SERVER="{{ base_url }}"

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
  linux)
    case "$ARCH" in
      x86_64|amd64) PLATFORM="linux-amd64" ;;
      aarch64|arm64) PLATFORM="linux-arm64" ;;
      *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac
    ;;
  darwin)
    case "$ARCH" in
      x86_64|amd64) PLATFORM="darwin-amd64" ;;
      arm64) PLATFORM="darwin-arm64" ;;
      *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac
    ;;
  *)
    echo "Unsupported OS: $OS" >&2
    echo "For Windows use: irm {{ base_url }}/agent/install.ps1 | iex" >&2
    exit 1
    ;;
esac

echo "Installing Vigil agent for $PLATFORM..."

# Download to a temp file and verify it before anything makes it executable.
# This binary becomes a root process under systemd, so an unverified download
# is a root compromise for anyone who can substitute the bytes in flight. The
# server publishes the digest in an X-Vigil-SHA256 response header; the agent's
# own self-updater already refuses to swap its binary without checking exactly
# this (agent/vigil_agent/executor.py), and the first install must not be the
# one step that skips it.
TMP_AGENT="$(mktemp)"
trap 'rm -f "$TMP_AGENT"' EXIT

HDRS="$(mktemp)"
curl -fsSL -D "$HDRS" -o "$TMP_AGENT" "${VIGIL_SERVER}/agent/download/${PLATFORM}/"
# Match with tolower($0) rather than awk's IGNORECASE: that is a gawk
# extension, and Debian and Ubuntu default to mawk, which ignores it in
# silence. Under mawk the pattern simply never matched the real, capitalised
# header, so this script refused every install on its main target platform.
EXPECTED_SHA="$(awk 'tolower($0) ~ /^x-vigil-sha256:/ {gsub(/\r/,"",$2); print tolower($2)}' "$HDRS")"
rm -f "$HDRS"

if [ -z "$EXPECTED_SHA" ]; then
  echo "ERROR: the server did not publish a SHA-256 for this agent binary." >&2
  echo "Refusing to install an unverified binary that would run as root." >&2
  echo "Upload the agent through Settings so its digest is recorded, or set" >&2
  echo "VIGIL_ALLOW_UNVERIFIED_AGENT=1 to override (not recommended)." >&2
  [ "${VIGIL_ALLOW_UNVERIFIED_AGENT:-}" = "1" ] || exit 1
else
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_SHA="$(sha256sum "$TMP_AGENT" | awk '{print tolower($1)}')"
  elif command -v shasum >/dev/null 2>&1; then
    ACTUAL_SHA="$(shasum -a 256 "$TMP_AGENT" | awk '{print tolower($1)}')"
  else
    echo "ERROR: neither sha256sum nor shasum is available to verify the download." >&2
    exit 1
  fi
  if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
    echo "ERROR: agent binary failed SHA-256 verification." >&2
    echo "  expected: $EXPECTED_SHA" >&2
    echo "  actual:   $ACTUAL_SHA" >&2
    echo "The download was corrupted or tampered with. Nothing was installed." >&2
    exit 1
  fi
  echo "Verified agent binary (sha256 $ACTUAL_SHA)."
fi

install -m 0755 "$TMP_AGENT" /usr/local/bin/vigil-agent

mkdir -p /etc/vigil
# 0755 regardless of umask: monitor mode runs as vigil-agent, which must traverse this dir (agent.yml itself stays 0600).
chmod 0755 /etc/vigil

# VIGIL_TOKEN ends up inside sed replacements: accept only a plain token.
if [ -n "${VIGIL_TOKEN:-}" ] && ! printf '%s' "$VIGIL_TOKEN" | grep -Eq '^[A-Za-z0-9_-]{16,128}$'; then
  echo "ERROR: VIGIL_TOKEN must be 16-128 letters, digits, '-' or '_'." >&2
  exit 1
fi

# A config the unprivileged service account could write is not trusted at all
# (SEC-3). Monitor-mode installs used to hand agent.yml to vigil-agent, and
# everything in it (server_url, mode, allowlist, paths) would otherwise flow
# into the service this installer creates, possibly as root. Reading single
# keys back out of it with sed is no answer either: YAML and sed can disagree
# about what a file says. So it is set aside and rebuilt below as on a fresh
# install, carrying over only the agent token, which is the host's identity.
if [ -f /etc/vigil/agent.yml ]; then
  CFG_OWNER="$(stat -c %u /etc/vigil/agent.yml 2>/dev/null || stat -f %u /etc/vigil/agent.yml 2>/dev/null || echo unknown)"
  CFG_PERM="$(stat -c %a /etc/vigil/agent.yml 2>/dev/null || stat -f %Lp /etc/vigil/agent.yml 2>/dev/null || echo 777)"
  if [ "$CFG_OWNER" != "0" ] || [ $(( 0$CFG_PERM & 022 )) -ne 0 ]; then
    if [ -z "${VIGIL_TOKEN:-}" ]; then
      KEPT_TOKEN="$(sed -n 's/^agent_token:[[:space:]]*//p' /etc/vigil/agent.yml | head -1 | tr -d '"'"'"' ')"
      # It came from a file root does not trust and goes into a sed command
      # run as root: only a plain token shape is carried over.
      if printf '%s' "$KEPT_TOKEN" | grep -Eq '^[A-Za-z0-9_-]{16,128}$'; then
        VIGIL_TOKEN="$KEPT_TOKEN"
      else
        echo "WARNING: the old agent token was not a valid token; a new one is generated and the host must be approved again." >&2
      fi
    fi
    mv -f /etc/vigil/agent.yml "/etc/vigil/agent.yml.untrusted.$(date +%s)"
    chmod 600 /etc/vigil/agent.yml.untrusted.* 2>/dev/null || true
    chown root /etc/vigil/agent.yml.untrusted.* 2>/dev/null || true
    echo "WARNING: /etc/vigil/agent.yml was writable by a non-root account, so none of its" >&2
    echo "settings are trusted. It was set aside and rebuilt; only the agent token was kept." >&2
  fi
fi

if [ ! -f /etc/vigil/agent.yml ]; then
  cat > /etc/vigil/agent.yml << 'EOF'
server_url: "REPLACE_WITH_SERVER_URL"
agent_token: "REPLACE_WITH_TOKEN"
mode: monitor
checkin_interval: 30
# Actions a managed-mode agent may run. Monitor mode ignores this list; these
# defaults are read-only, so switching to managed can hunt and inventory at once.
allowlist:
  - app_inventory
  - check_service
  - container_logs
  - hunt_content
  - hunt_file
  - hunt_package
  - hunt_port
  - hunt_process
  - hunt_registry
  - hunt_service
EOF
  sed -i.bak "s|REPLACE_WITH_SERVER_URL|${VIGIL_SERVER}|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak

  if [ -n "${VIGIL_TOKEN:-}" ]; then
    sed -i.bak "s|REPLACE_WITH_TOKEN|${VIGIL_TOKEN}|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak
    echo "Agent token configured from VIGIL_TOKEN."
  elif [ -n "${VIGIL_ENROLL_TOKEN:-}" ]; then
    # Post-rebuild re-enrolment (docs/reprovisioning.md §4.3). We generate the
    # long-lived agent token here and exchange the one-time enrolment token
    # for the right to bind it to the existing host record. Doing it this way
    # round means the answer file only ever carried a single-use credential,
    # already consumed by the time anyone could replay it.
    NEW_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    ENROLL_BODY="{\"enroll_token\":\"${VIGIL_ENROLL_TOKEN}\",\"agent_token\":\"${NEW_TOKEN}\",\"hostname\":\"$(hostname)\"}"
    if curl -fsS -X POST "${VIGIL_SERVER}/api/v1/reprovision/enroll" \
         -H "Content-Type: application/json" -d "${ENROLL_BODY}" >/dev/null; then
      sed -i.bak "s|REPLACE_WITH_TOKEN|${NEW_TOKEN}|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak
      echo "Re-enrolled against the existing host record."
    else
      # Fail loudly: a rebuilt machine that silently fails to enrol is
      # invisible in the console and looks like a dead host.
      echo "ERROR: re-enrolment was rejected by ${VIGIL_SERVER}." >&2
      echo "The one-time token may have expired or already been used." >&2
      exit 1
    fi
  else
    # Generate the token here rather than leaving a placeholder. The server
    # takes whatever token the agent presents and stores it verbatim, so a
    # literal "REPLACE_WITH_TOKEN" left in place becomes a working credential
    # that is published in this very script. Anyone who could reach the API
    # could then authenticate as this host, read its task queue, and — because
    # register() is idempotent on the token — enrol a second machine that
    # inherits this host's already-approved identity without any admin action.
    # The reprovision branch above has generated a real token all along; this
    # branch simply never did.
    NEW_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    sed -i.bak "s|REPLACE_WITH_TOKEN|${NEW_TOKEN}|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak
    echo "Config written to /etc/vigil/agent.yml with a generated agent token."
  fi
elif [ -n "${VIGIL_TOKEN:-}" ]; then
  # Re-adding a machine: keep its config but take the token the wizard is waiting for.
  sed -i.bak "s|^agent_token:.*|agent_token: \"${VIGIL_TOKEN}\"|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak
  echo "Existing config kept; agent token replaced from VIGIL_TOKEN."
fi

# ── Server key and mode (SEC-3) ─────────────────────────────────────────────
# The server's public key arrives in this script, over the same TLS download as
# the agent binary, and is written into agent.yml. The agent then accepts only
# that key: no trust on first use, and a pin file in the data directory (owned
# by the unprivileged service account in monitor mode) cannot override it.
SERVER_PUBLIC_KEY="{{ public_key }}"
if [ -n "$SERVER_PUBLIC_KEY" ]; then
  if grep -q '^server_public_key:' /etc/vigil/agent.yml; then
    sed -i.bak "s|^server_public_key:.*|server_public_key: \"${SERVER_PUBLIC_KEY}\"|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak
  else
    printf 'server_public_key: "%s"\n' "$SERVER_PUBLIC_KEY" >> /etc/vigil/agent.yml
  fi
fi

# agent.yml is root-owned by now (an untrusted one was rebuilt above), so the
# mode in it was written by root. VIGIL_MODE lets the admin set it here.
CFG_MODE="$(sed -n 's/^mode:[[:space:]]*//p' /etc/vigil/agent.yml 2>/dev/null | head -1 | tr -d '"'"'"' ')"
if [ -n "${VIGIL_MODE:-}" ]; then
  case "$VIGIL_MODE" in
    monitor|managed|full_control) CFG_MODE="$VIGIL_MODE" ;;
    *) echo "ERROR: VIGIL_MODE must be monitor, managed or full_control" >&2; exit 1 ;;
  esac
fi
case "${CFG_MODE:-monitor}" in
  monitor|managed|full_control) ;;
  *) echo "WARNING: unknown mode '${CFG_MODE}' in agent.yml; using monitor" >&2; CFG_MODE="monitor" ;;
esac
CFG_MODE="${CFG_MODE:-monitor}"
if grep -q '^mode:' /etc/vigil/agent.yml; then
  sed -i.bak "s|^mode:.*|mode: ${CFG_MODE}|" /etc/vigil/agent.yml && rm -f /etc/vigil/agent.yml.bak
else
  printf 'mode: %s\n' "$CFG_MODE" >> /etc/vigil/agent.yml
fi
# Root owns the config in every mode; monitor mode gets group read below.
chown root /etc/vigil/agent.yml
chmod 600 /etc/vigil/agent.yml

# ── Service installation ────────────────────────────────────────────────────

if [ "$OS" = "linux" ] && command -v systemctl >/dev/null 2>&1; then
  # Carry the installing shell's egress proxy into the service env.
  # systemd units don't inherit a login shell's environment, so on a
  # proxied network the agent (and any task that shells out to curl/wget,
  # e.g. installing Trivy) can't reach the internet even though the host
  # can. Requires the proxy vars to be present at install time — run the
  # installer with `sudo -E` (or as a root shell that already has them).
  PROXY_LINES=""
  _hp="${HTTP_PROXY:-${http_proxy:-}}"
  _hsp="${HTTPS_PROXY:-${https_proxy:-}}"
  _np="${NO_PROXY:-${no_proxy:-}}"
  if [ -n "$_hp" ] || [ -n "$_hsp" ]; then
    # Keep loopback + the Vigil server itself direct, then append any
    # operator-provided no_proxy entries.
    _server_host=$(printf '%s' "$VIGIL_SERVER" | sed -e 's|^https\?://||' -e 's|[:/].*$||')
    _np_full="localhost,127.0.0.1,${_server_host}${_np:+,$_np}"
    [ -n "$_hp" ]  && PROXY_LINES="${PROXY_LINES}Environment=HTTP_PROXY=${_hp}
Environment=http_proxy=${_hp}
"
    [ -n "$_hsp" ] && PROXY_LINES="${PROXY_LINES}Environment=HTTPS_PROXY=${_hsp}
Environment=https_proxy=${_hsp}
"
    PROXY_LINES="${PROXY_LINES}Environment=NO_PROXY=${_np_full}
Environment=no_proxy=${_np_full}"
    echo "Detected proxy — baking egress config into the agent service."
  fi

  # Only a mode that executes tasks needs root. A monitor-mode agent reads
  # /proc and /sys and posts the numbers, which any user can do — the one
  # thing it loses unprivileged is dmidecode's manufacturer/model, and that
  # call already degrades to an absent field rather than failing.
  #
  # This matters because monitor is the mode this installer writes by default,
  # so most agents were running as root to do a job that needs none of it.
  AGENT_MODE="$CFG_MODE"

  RUN_AS=""
  HARDENING=""
  if [ "$AGENT_MODE" = "monitor" ]; then
    if ! id vigil-agent >/dev/null 2>&1; then
      useradd --system --no-create-home --shell /usr/sbin/nologin vigil-agent 2>/dev/null || true
    fi
    if id vigil-agent >/dev/null 2>&1; then
      mkdir -p /var/lib/vigil-agent
      chown -R vigil-agent /var/lib/vigil-agent
      # Readable by the service account, writable only by root (SEC-3).
      chown root:vigil-agent /etc/vigil/agent.yml 2>/dev/null || true
      chmod 640 /etc/vigil/agent.yml 2>/dev/null || true
      RUN_AS="User=vigil-agent"
      # Safe for a process that only reads counters. Deliberately NOT applied
      # to managed or full_control: an agent whose job is systemctl and
      # apt-get needs to write /etc and /usr, and ProtectSystem would leave it
      # failing every task it was asked to run.
      HARDENING="NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
MemoryDenyWriteExecute=no
ReadWritePaths=/var/lib/vigil-agent
CapabilityBoundingSet="
      echo "Monitor mode: running the agent as the unprivileged 'vigil-agent' user."
      # Containers (M11): the engine socket is root's, and the docker group is
      # root by another name, so a monitor-mode agent reads containers through
      # a small root service that answers GETs of the list, inspect, stats and
      # logs only — never a start, stop or exec.
      if [ -S /var/run/docker.sock ] || [ -S /run/podman/podman.sock ]; then
        cat > /etc/systemd/system/vigil-engine-proxy.service << 'PROXYEOF'
[Unit]
Description=Vigil read-only container engine socket (monitor mode)
After=docker.service podman.socket
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/vigil-agent --engine-proxy
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RuntimeDirectory=vigil
RuntimeDirectoryPreserve=yes
ReadWritePaths=/run/vigil -/var/run/docker.sock -/run/podman

[Install]
WantedBy=multi-user.target
PROXYEOF
        systemctl daemon-reload
        systemctl enable --now vigil-engine-proxy >/dev/null 2>&1 || true
        echo "Containers: monitor mode reads them through a read-only engine socket."
      fi
    fi
  else
    # Root, because the mode's whole purpose needs it — but still deny the
    # gaining of *new* privileges through setuid binaries, which a task that
    # legitimately runs as root never needs to do.
    HARDENING="NoNewPrivileges=yes
ProtectKernelModules=yes
RestrictRealtime=yes
LockPersonality=yes"
    echo "Mode '$AGENT_MODE' executes tasks, so the agent runs as root."
    echo "  Switch to monitor mode to run it unprivileged."
  fi

  cat > /etc/systemd/system/vigil-agent.service << EOF
[Unit]
Description=Vigil Monitoring Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/vigil-agent
Restart=always
RestartSec=10
${RUN_AS}
${HARDENING}
${PROXY_LINES}

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable vigil-agent
  # A reinstall over a running agent must restart it: otherwise the old process
  # keeps the old binary, privileges and config (including a config this run
  # just rebuilt because it was not trusted) until something else restarts it.
  if [ -n "${VIGIL_TOKEN:-}" ] || systemctl is-active --quiet vigil-agent; then
    if systemctl is-active --quiet vigil-engine-proxy; then systemctl restart vigil-engine-proxy; fi
    systemctl restart vigil-agent
    echo "Vigil agent installed and started."
    echo "Approve this host in Vigil Settings > Enrollment Queue."
  else
    echo "Vigil agent installed with a generated agent token."
    echo "  1. systemctl start vigil-agent"
    echo "  2. Approve the host in Vigil Settings > Enrollment Queue"
  fi

elif [ "$OS" = "darwin" ]; then
  PLIST_PATH="/Library/LaunchDaemons/com.susquehannasyntax.vigil-agent.plist"
  cat > "$PLIST_PATH" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.susquehannasyntax.vigil-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/vigil-agent</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/vigil-agent.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/vigil-agent.log</string>
</dict>
</plist>
EOF
  launchctl load "$PLIST_PATH"
  if [ -n "${VIGIL_TOKEN:-}" ]; then
    launchctl start com.susquehannasyntax.vigil-agent
    echo "Vigil agent installed and started."
    echo "Approve this host in Vigil Settings > Enrollment Queue."
  else
    echo "Vigil agent installed with a generated agent token."
    echo "  1. launchctl start com.susquehannasyntax.vigil-agent"
    echo "  2. Approve the host in Vigil Settings > Enrollment Queue"
  fi

else
  echo "Vigil agent installed to /usr/local/bin/vigil-agent"
  echo "Edit /etc/vigil/agent.yml and start the agent manually."
fi
{% endautoescape %}
