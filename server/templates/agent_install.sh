{% autoescape off %}#!/bin/bash
# Vigil Agent installer — {{ base_url }}
# Usage: curl -fsSL {{ base_url }}/agent/install.sh | sudo bash
#   or:  VIGIL_TOKEN=<token> curl -fsSL {{ base_url }}/agent/install.sh | sudo bash
set -e

VIGIL_SERVER="{{ base_url }}"

# Detect OS and architecture
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

if [ "$OS" != "linux" ]; then
  echo "Unsupported OS: $OS (only Linux is supported at this time)" >&2
  exit 1
fi

case "$ARCH" in
  x86_64|amd64) PLATFORM="linux-amd64" ;;
  aarch64|arm64) PLATFORM="linux-arm64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
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
  sed -i "s|REPLACE_WITH_SERVER_URL|${VIGIL_SERVER}|" /etc/vigil/agent.yml

  # If VIGIL_TOKEN env var is set (enrollment wizard flow), inject it automatically
  if [ -n "${VIGIL_TOKEN:-}" ]; then
    sed -i "s|REPLACE_WITH_TOKEN|${VIGIL_TOKEN}|" /etc/vigil/agent.yml
    echo "Agent token configured from VIGIL_TOKEN."
  else
    echo "Config written to /etc/vigil/agent.yml — set agent_token before starting."
  fi
fi

if command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/vigil-agent.service << 'EOF'
[Unit]
Description=Vigil Monitoring Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/vigil-agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable vigil-agent

  if [ -n "${VIGIL_TOKEN:-}" ]; then
    systemctl start vigil-agent
    echo ""
    echo "Vigil agent installed and started."
    echo "  Approve this host in Vigil Settings > Enrollment Queue."
  else
    echo ""
    echo "Vigil agent installed."
    echo "  1. Edit /etc/vigil/agent.yml and set agent_token"
    echo "  2. systemctl start vigil-agent"
    echo "  3. Approve the host in Vigil Settings > Enrollment Queue"
  fi
else
  echo "Vigil agent installed to /usr/local/bin/vigil-agent"
  echo "Edit /etc/vigil/agent.yml and start the agent manually."
fi
{% endautoescape %}
