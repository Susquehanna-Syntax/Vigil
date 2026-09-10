FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/vigil-agent

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

# No USER directive, deliberately. The agent's whole job is systemctl,
# apt-get/dnf, ufw/firewall-cmd and useradd on the machine it manages — it
# needs root there, and dropping privileges would leave it unable to do the
# thing it exists to do. The security model does not rest on the agent being
# unprivileged; it rests on the agent deciding what it will execute
# (mode + local allowlist + Ed25519 signature), which phase 07b of the M6
# audit traced end to end.

COPY vigil_agent/ vigil_agent/

# Config is mounted in at runtime: -v /etc/vigil/agent.yml:/etc/vigil/agent.yml
# Or set VIGIL_CONFIG_PATH to override.

ENV VIGIL_CONFIG_PATH=/etc/vigil/agent.yml

CMD ["python", "-m", "vigil_agent", "-c", "/etc/vigil/agent.yml"]
