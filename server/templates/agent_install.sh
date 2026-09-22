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

if [ ! -f /etc/vigil/agent.yml ]; then
  cat > /etc/vigil/agent.yml << 'EOF'
server_url: "REPLACE_WITH_SERVER_URL"
agent_token: "REPLACE_WITH_TOKEN"
mode: monitor
checkin_interval: 30
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
fi

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
  AGENT_MODE="$(sed -n 's/^mode:[[:space:]]*//p' /etc/vigil/agent.yml 2>/dev/null | head -1)"
  AGENT_MODE="${AGENT_MODE:-monitor}"

  RUN_AS=""
  HARDENING=""
  if [ "$AGENT_MODE" = "monitor" ]; then
    if ! id vigil-agent >/dev/null 2>&1; then
      useradd --system --no-create-home --shell /usr/sbin/nologin vigil-agent 2>/dev/null || true
    fi
    if id vigil-agent >/dev/null 2>&1; then
      mkdir -p /var/lib/vigil-agent
      chown -R vigil-agent /var/lib/vigil-agent
      chown vigil-agent /etc/vigil/agent.yml 2>/dev/null || true
      chmod 600 /etc/vigil/agent.yml 2>/dev/null || true
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
  if [ -n "${VIGIL_TOKEN:-}" ]; then
    systemctl start vigil-agent
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
