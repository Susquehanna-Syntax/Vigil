# Vigil

**Smart, self-hosted infrastructure monitoring and endpoint management.**

A [Susquehanna Syntax](https://sqsy.dev) product.

---

## Quick Start (Local Dev)

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate --settings=vigil.settings_local
.venv/bin/python manage.py runserver --settings=vigil.settings_local
```

Open [http://localhost:8000/api/v1/health/](http://localhost:8000/api/v1/health/) — you should see:

```json
{"status": "ok"}
```

To access the Django admin:

```bash
.venv/bin/python manage.py createsuperuser --settings=vigil.settings_local
```

Then visit [http://localhost:8000/admin/](http://localhost:8000/admin/).

## Quick Start (Docker)

```bash
cp .env.example .env
docker compose up -d
```

This brings up the full stack: Django, PostgreSQL + TimescaleDB, Redis, Celery worker, and Celery beat.

## Project Layout

```
Vigil/
├── docker-compose.yml        # Full production-like stack
├── .env.example              # Environment variable template
└── server/
    ├── Dockerfile
    ├── requirements.txt
    ├── manage.py
    ├── vigil/                # Django project config
    │   ├── settings.py       # Production settings (Postgres, Redis)
    │   ├── settings_local.py # Local dev settings (SQLite, no Redis)
    │   ├── celery.py
    │   └── urls.py
    └── apps/
        ├── hosts/            # Agent enrollment and host tracking
        ├── metrics/          # Time-series metric storage
        ├── alerts/           # Alert rules and fired alerts
        └── tasks/            # Signed remote task execution
```

## API Endpoints

### Agent-facing (Bearer token auth)

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/checkin` | Metric ingest + task dispatch |
| `POST` | `/api/v1/tasks/result/` | Report task execution outcome |

### Admin-facing (session or token auth)

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/hosts/` | List enrolled hosts |
| `GET` | `/api/v1/hosts/{id}/` | Host detail |
| `POST` | `/api/v1/hosts/{id}/approve/` | Approve a pending enrollment |
| `POST` | `/api/v1/hosts/{id}/poll/` | Request immediate check-in |
| `GET` | `/api/v1/metrics/{host_id}/{category}/{metric}/` | Query metric history |
| `GET` | `/api/v1/alerts/` | List alerts (`?state=firing`, `acknowledged`, or `resolved`) |
| `POST` | `/api/v1/alerts/{id}/acknowledge/` | Acknowledge a firing alert |
| `POST` | `/api/v1/alerts/{id}/silence/` | Silence a firing alert |
| `GET` | `/api/v1/tasks/` | List tasks (`?host=id&state=pending`) |
| `GET` | `/api/v1/health/` | Health check |

## License

AGPLv3
