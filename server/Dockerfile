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

# Record which agent version this build produced. The server image has the
# binaries but not the agent source, and it needs the version to tell an
# outdated host from a current one — this is the only place both are present.
RUN python -c "exec(open('vigil_agent/__version__.py').read()); print(__version__)" \
    > /build/AGENT_VERSION

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

# Stamp the agent version beside the binaries. settings._detect_agent_version
# reads this, so the expected agent version travels with the image instead of
# living in an environment variable somebody has to remember to bump. Same
# gap-filling rule as the binaries above: if CI already dropped a VERSION next
# to its cross-platform artifacts, that one is authoritative.
COPY --from=agent-builder /build/AGENT_VERSION /tmp/agent-version
RUN if [ ! -f /app/agent_dist/VERSION ]; then \
        cp /tmp/agent-version /app/agent_dist/VERSION; \
    fi && \
    rm -f /tmp/agent-version

# VIGIL_SKIP_STARTUP_CHECKS=1 lets manage.py boot without VIGIL_SIGNING_KEY_SEED.
# Build-time collectstatic doesn't need a real signing key — the runtime env
# (compose/Portainer) supplies the seed. Required since DEBUG defaults to false.
RUN VIGIL_SKIP_STARTUP_CHECKS=1 python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
# --timeout 1800 (default is 30s): apps.reprovision.views.image_upload
# streams a multipart ISO upload synchronously inside the request/response
# cycle so the operator gets a real success/failure instead of a
# fire-and-forget job. A multi-gigabyte upload over a slow link can easily
# run past 30s, and the default kills the worker mid-upload — the arbiter
# has no idea a legitimate transfer is still in progress. Do not trim this
# back without an alternative (e.g. a background upload endpoint) in place.
CMD ["gunicorn", "vigil.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "1800"]
