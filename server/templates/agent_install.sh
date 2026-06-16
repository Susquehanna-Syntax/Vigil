{% autoescape off %}#!/bin/bash
# Vigil Agent installer — {{ base_url }}
# Linux / macOS
# Usage: curl -fsSL {{ base_url }}/agent/install.sh | sudo bash
#   or:  VIGIL_TOKEN=<token> curl -fsSL {{ base_url }}/agent/install.sh | sudo bash
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

curl -fsSL -o /usr/local/bin/vigil-agent "${VIGIL_SERVER}/agent/download/${PLATFORM}/"
chmod +x /usr/local/bin/vigil-agent

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
  else
    echo "Config written to /etc/vigil/agent.yml — set agent_token before starting."
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
    echo "Vigil agent installed."
    echo "  1. Edit /etc/vigil/agent.yml and set agent_token"
    echo "  2. systemctl start vigil-agent"
    echo "  3. Approve the host in Vigil Settings > Enrollment Queue"
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
    echo "Vigil agent installed."
    echo "  1. Edit /etc/vigil/agent.yml and set agent_token"
    echo "  2. launchctl start com.susquehannasyntax.vigil-agent"
    echo "  3. Approve the host in Vigil Settings > Enrollment Queue"
  fi

else
  echo "Vigil agent installed to /usr/local/bin/vigil-agent"
  echo "Edit /etc/vigil/agent.yml and start the agent manually."
fi
{% endautoescape %}
