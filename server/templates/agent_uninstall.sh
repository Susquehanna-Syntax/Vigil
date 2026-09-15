{% autoescape off %}#!/usr/bin/env bash
# Vigil agent uninstaller — {{ base_url }}
#
#   curl -fsSL {{ base_url }}/agent/uninstall.sh | sudo bash
#
# Removes the agent, its service definition, its config and its state. The
# host stops reporting immediately; its record stays in Vigil until you delete
# it there, so history is not lost by uninstalling.
#
# Every path below is a literal. Nothing is interpolated into an rm, because
# an uninstaller running as root with an empty variable is how people lose
# machines.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run this with sudo." >&2
  exit 1
fi

KEEP_CONFIG=0
for arg in "$@"; do
  case "$arg" in
    --keep-config) KEEP_CONFIG=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
removed=0

say() { echo "  removed $1"; removed=$((removed + 1)); }

if [ "$OS" = "linux" ]; then
  if systemctl list-unit-files vigil-agent.service >/dev/null 2>&1; then
    systemctl stop vigil-agent    >/dev/null 2>&1 || true
    systemctl disable vigil-agent >/dev/null 2>&1 || true
  fi
  if [ -f /etc/systemd/system/vigil-agent.service ]; then
    rm -f /etc/systemd/system/vigil-agent.service
    systemctl daemon-reload >/dev/null 2>&1 || true
    say "the systemd unit"
  fi
elif [ "$OS" = "darwin" ]; then
  if [ -f /Library/LaunchDaemons/com.susquehannasyntax.vigil-agent.plist ]; then
    launchctl unload /Library/LaunchDaemons/com.susquehannasyntax.vigil-agent.plist >/dev/null 2>&1 || true
    rm -f /Library/LaunchDaemons/com.susquehannasyntax.vigil-agent.plist
    say "the launchd daemon"
  fi
  rm -f /var/log/vigil-agent.log
fi

[ -f /usr/local/bin/vigil-agent ] && { rm -f /usr/local/bin/vigil-agent; say "/usr/local/bin/vigil-agent"; }

# State: the nonce store and the pinned server key. Removing it is the point —
# a re-install should not silently inherit a key pin from a previous life.
[ -d /var/lib/vigil-agent ] && { rm -rf /var/lib/vigil-agent; say "/var/lib/vigil-agent"; }

if [ "$KEEP_CONFIG" -eq 1 ]; then
  echo "  kept /etc/vigil/agent.yml (--keep-config)"
else
  [ -f /etc/vigil/agent.yml ] && { rm -f /etc/vigil/agent.yml; say "/etc/vigil/agent.yml"; }
  rm -f /etc/vigil/agent.yml.bak
  # Only if empty: an operator may keep scripts for execute_script in here.
  rmdir /etc/vigil 2>/dev/null && echo "  removed /etc/vigil (was empty)" || true
fi

# The unprivileged account monitor mode runs as. Left alone if anything else
# still owns files it would need.
if id vigil-agent >/dev/null 2>&1; then
  userdel vigil-agent >/dev/null 2>&1 && say "the vigil-agent user" || \
    echo "  left the vigil-agent user in place (still in use)"
fi

echo ""
if [ "$removed" -eq 0 ]; then
  echo "Nothing to remove — no Vigil agent found on this host."
else
  echo "Vigil agent uninstalled ($removed item(s) removed)."
  echo "The host record remains in Vigil; delete it there if you want the"
  echo "history gone as well."
fi
{% endautoescape %}
