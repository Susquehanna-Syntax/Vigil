#!/usr/bin/env bash
# Run the agent from this checkout, as root, inside a stock OS image on the
# host network (sprint M2 phase 2). scripts/agent-e2e.py appends "-c CONFIG".
#
#   scripts/agent-in-container.sh debian:12 -c /work/agent.yml
#
# The config path must sit under a directory mounted at the same path inside
# (AGENT_E2E_WORK), so the agent finds it and its data dir where the driver
# put them.
set -euo pipefail
image="$1"; shift
repo="$(cd "$(dirname "$0")/.." && pwd)"
work="${AGENT_E2E_WORK:?set AGENT_E2E_WORK to the --workdir given to agent-e2e.py}"
exec docker run --rm --network host -v "$repo/agent:/agent:ro" -v "$work:$work" -w /agent "$image" sh -c '
  set -e
  if command -v apt-get >/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3 python3-venv >/dev/null
  elif command -v dnf >/dev/null; then
    dnf install -y -q python3.12 python3.12-pip >/dev/null && ln -sf /usr/bin/python3.12 /usr/local/bin/python3
  fi
  python3 -m venv /tmp/venv
  /tmp/venv/bin/pip install -q -r /agent/requirements.txt
  exec /tmp/venv/bin/python -m vigil_agent "$@"
' agent "$@"
