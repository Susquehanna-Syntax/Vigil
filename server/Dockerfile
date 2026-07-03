# Stage 1: Build agent binary with PyInstaller
FROM python:3.12-slim AS agent-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends binutils && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pyinstaller psutil requests PyNaCl PyYAML

WORKDIR /build
COPY agent/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY agent/vigil_agent/ vigil_agent/
COPY agent/vigil_agent_main.py vigil_agent_main.py
RUN pyinstaller --onefile --name vigil-agent \
    --collect-all cffi \
    --collect-all nacl \
    vigil_agent_main.py

# Stage 2: Server image
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ .

# Bundle the agent binaries — served via /agent/download/<platform>/.
# CI drops cross-platform binaries (linux-amd64/arm64, darwin, windows) into
# server/agent_dist/ before the image build, so they arrive via COPY server/ .
# above. The agent-builder stage output only fills the gap for the image's
# native arch on local builds, where no CI artifacts exist. TARGETARCH is
# amd64 or arm64 depending on which platform buildx is producing — naming
# the fallback correctly keeps an arm64 image from serving an arm64 binary
# under the linux-amd64 name.
ARG TARGETARCH
RUN mkdir -p /app/agent_dist
COPY --from=agent-builder /build/dist/vigil-agent /tmp/vigil-agent-native
RUN if [ ! -f "/app/agent_dist/vigil-agent-linux-${TARGETARCH}" ]; then \
        cp /tmp/vigil-agent-native "/app/agent_dist/vigil-agent-linux-${TARGETARCH}"; \
    fi && \
    rm -f /tmp/vigil-agent-native && \
    chmod +x /app/agent_dist/vigil-agent-linux-* 2>/dev/null || true

# VIGIL_SKIP_STARTUP_CHECKS=1 lets manage.py boot without VIGIL_SIGNING_KEY_SEED.
# Build-time collectstatic doesn't need a real signing key — the runtime env
# (compose/Portainer) supplies the seed. Required since DEBUG defaults to false.
RUN VIGIL_SKIP_STARTUP_CHECKS=1 python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "vigil.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
