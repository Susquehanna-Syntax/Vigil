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

## License

AGPLv3
