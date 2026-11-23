#!/usr/bin/env bash
# Install the agent the documented way inside a stock OS image, then run the
# binary that install.sh put in place (sprint M2): unlike agent-in-container.sh,
# which runs this checkout's source, this proves that install.sh fetches and
# verifies the build the server serves, and that the binary starts on that
# distribution's libc. scripts/agent-e2e.py appends "-c CONFIG".
#
#   scripts/agent-installed-in-container.sh debian:12 http://127.0.0.1:8010 -c /work/agent.yml
#
# There is no systemd in the container, so install.sh writes the binary and a
# config and stops there; the driver's config is the one the agent runs with.
set -euo pipefail
image="$1"; server="$2"; shift 2
work="${AGENT_E2E_WORK:?set AGENT_E2E_WORK to the --workdir given to agent-e2e.py}"
exec docker run --rm --network host -v "$work:$work" -e VIGIL_SERVER="$server" "$image" sh -c '
  set -e
  if command -v apt-get >/dev/null; then
    apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null
  elif command -v dnf >/dev/null; then
    command -v curl >/dev/null || dnf install -y -q curl >/dev/null
  fi
  curl -fsSL "$VIGIL_SERVER/agent/install.sh" | bash >&2
  test -x /usr/local/bin/vigil-agent
  test -f /etc/vigil/agent.yml
  exec /usr/local/bin/vigil-agent "$@"
' agent "$@"
