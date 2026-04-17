FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/vigil-agent

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vigil_agent/ vigil_agent/

# Config is mounted in at runtime: -v /etc/vigil/agent.yml:/etc/vigil/agent.yml
# Or set VIGIL_CONFIG_PATH to override.

ENV VIGIL_CONFIG_PATH=/etc/vigil/agent.yml

CMD ["python", "-m", "vigil_agent", "-c", "/etc/vigil/agent.yml"]
