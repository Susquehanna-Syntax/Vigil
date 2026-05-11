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

# Bundle the agent binary — served via /agent/download/<platform>/
RUN mkdir -p /app/agent_dist
COPY --from=agent-builder /build/dist/vigil-agent /app/agent_dist/vigil-agent-linux-amd64
RUN chmod +x /app/agent_dist/vigil-agent-linux-amd64

RUN python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "vigil.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
