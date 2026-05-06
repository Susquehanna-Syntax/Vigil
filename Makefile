# Vigil — Makefile
# Usage: make <target>
#
# Common targets:
#   make setup     — first-time setup (copy .env, generate keys)
#   make up        — start all services
#   make down      — stop all services
#   make logs      — follow all logs
#   make shell     — Django shell in running web container
#   make build     — build Docker images locally
#   make push      — push server image to DockerHub (susquehannasyntax/vigil)
#   make test      — run Django test suite

DOCKER_IMAGE ?= susquehannasyntax/vigil
VERSION      ?= latest

.PHONY: help setup up down logs shell build push test migrate superuser \
        gen-key gen-secret worker-logs beat-logs db-shell clean

help:
	@echo ""
	@echo "  Vigil — self-hosted infrastructure monitoring"
	@echo ""
	@echo "  First time:   make setup && make up"
	@echo "  Daily use:    make up / make down / make logs"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── First-time setup ──────────────────────────────────────────────────────────

setup: ## Copy .env.example → .env and generate keys
	@if [ -f .env ]; then \
		echo "  .env already exists — skipping copy"; \
	else \
		cp .env.example .env; \
		echo "  Created .env from .env.example"; \
	fi
	@echo ""
	@echo "  ┌─────────────────────────────────────────────────────────┐"
	@echo "  │ Next steps:                                             │"
	@echo "  │  1. Edit .env and set passwords + DJANGO_SECRET_KEY     │"
	@echo "  │  2. Run 'make gen-key' to generate VIGIL_SIGNING_KEY_SEED│"
	@echo "  │  3. Run 'make up' to start all services                 │"
	@echo "  └─────────────────────────────────────────────────────────┘"

gen-key: ## Generate and print a VIGIL_SIGNING_KEY_SEED value
	@echo ""
	@echo "  Ed25519 signing key seed (base64):"
	@python3 -c "import base64; from nacl.signing import SigningKey; print('  ' + base64.b64encode(bytes(SigningKey.generate())).decode())"
	@echo ""
	@echo "  Paste this value into .env as VIGIL_SIGNING_KEY_SEED="

gen-secret: ## Generate and print a DJANGO_SECRET_KEY value
	@echo ""
	@echo "  Django secret key:"
	@python3 -c "import secrets; print('  ' + secrets.token_urlsafe(50))"
	@echo ""
	@echo "  Paste this value into .env as DJANGO_SECRET_KEY="

# ── Docker Compose operations ─────────────────────────────────────────────────

up: ## Start all services (builds if needed)
	docker compose up -d --build
	@echo ""
	@echo "  Vigil is starting up. Dashboard: http://localhost:$$(grep VIGIL_PORT .env 2>/dev/null | cut -d= -f2 || echo 8000)"
	@echo "  Follow logs: make logs"

down: ## Stop all services
	docker compose down

restart: ## Restart all services
	docker compose restart

logs: ## Follow logs from all services
	docker compose logs -f

web-logs: ## Follow web server logs only
	docker compose logs -f web

worker-logs: ## Follow Celery worker logs
	docker compose logs -f celery-worker

beat-logs: ## Follow Celery beat logs
	docker compose logs -f celery-beat

build: ## Build Docker images (no cache)
	docker compose build --no-cache

# ── Docker image publishing ───────────────────────────────────────────────────

push: ## Push server image to DockerHub (run: docker login first)
	docker build -t $(DOCKER_IMAGE):$(VERSION) ./server
	docker push $(DOCKER_IMAGE):$(VERSION)
	@if [ "$(VERSION)" != "latest" ]; then \
		docker tag $(DOCKER_IMAGE):$(VERSION) $(DOCKER_IMAGE):latest; \
		docker push $(DOCKER_IMAGE):latest; \
	fi
	@echo "  Pushed $(DOCKER_IMAGE):$(VERSION)"

# ── Django management ─────────────────────────────────────────────────────────

shell: ## Open Django shell in the running web container
	docker compose exec web python manage.py shell

migrate: ## Run Django migrations
	docker compose exec web python manage.py migrate

superuser: ## Create a Django superuser interactively
	docker compose exec web python manage.py createsuperuser

test: ## Run the Django test suite
	docker compose exec web python manage.py test

db-shell: ## Open a psql shell inside the database container
	docker compose exec db psql -U $${POSTGRES_USER:-vigil} $${POSTGRES_DB:-vigil}

# ── Agent (local Python, for development) ─────────────────────────────────────

agent-run: ## Run the agent locally (uses /tmp/vigil-agent.yml)
	@if [ ! -f /tmp/vigil-agent.yml ]; then \
		cp agent/config.example.yml /tmp/vigil-agent.yml; \
		echo "  Created /tmp/vigil-agent.yml — edit it and re-run"; \
		exit 1; \
	fi
	cd agent && python -m vigil_agent -c /tmp/vigil-agent.yml

agent-install: ## Install agent Python dependencies
	cd agent && pip install -r requirements.txt

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean: ## Remove stopped containers and dangling images
	docker compose down --remove-orphans
	docker image prune -f

clean-all: ## Full clean including volumes (DESTROYS DATABASE)
	@echo "  WARNING: This will destroy the database. Press Ctrl-C to abort."
	@sleep 5
	docker compose down --volumes --remove-orphans
	docker image prune -f
