AXIOM_UID := $(shell id -u)
AXIOM_GID := $(shell id -g)
DOCKER_GID := $(shell stat -c %g /var/run/docker.sock 2>/dev/null || echo 999)
AXIOM_WORKSPACES_DIR ?= $(HOME)/.axiom/work
export AXIOM_UID AXIOM_GID DOCKER_GID AXIOM_WORKSPACES_DIR

.PHONY: report worker-tokens workers workers-down db up down logs test test-docker test-live sandbox-image lint fmt migrate seed smoke demo-restart

db:
	docker compose up -d --wait db

up:
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f api

test: db
	uv run pytest

sandbox-image:
	docker compose --profile tools build sandbox
	docker compose build egress-proxy
	docker compose up -d --wait egress-proxy

test-docker: db sandbox-image
	uv run pytest -m docker

test-live: db sandbox-image
	set -a && . ./.env && set +a && uv run pytest -m live -s

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt:
	uv run ruff format .
	uv run ruff check --fix .

migrate: db
	uv run python -m axiom.state.migrate

seed: migrate
	uv run python -m axiom.registry.seed

smoke:
	scripts/compose_smoke.sh

demo-restart: up
	scripts/demo_restart.sh

# Issue API tokens for the developer and reviewer workers and store them in .env (once).
worker-tokens: up
	@grep -q '^AXIOM_MODEL_PROXY_ADMIN_TOKEN=.' .env 2>/dev/null || { \
	  sed -i '/^AXIOM_MODEL_PROXY_ADMIN_TOKEN=/d' .env 2>/dev/null; \
	  echo "AXIOM_MODEL_PROXY_ADMIN_TOKEN=$$(openssl rand -hex 32)" >> .env; \
	  echo "AXIOM_MODEL_PROXY_ADMIN_TOKEN added to .env"; }
	@for a in DEV01:DEV-01 DEV02:DEV-02 REV01:REV-01 SEC01:SEC-01; do \
	  var=AXIOM_$${a%%:*}_TOKEN; key=$${a#*:}; \
	  if grep -q "^$$var=" .env 2>/dev/null; then echo "$$var already in .env"; \
	  else tok=$$(docker compose exec -T api python -m axiom.registry.tokens issue agent:$$key --label worker); \
	    echo "$$var=$$tok" >> .env; echo "$$var added to .env"; fi; \
	done

workers: sandbox-image
	mkdir -p $(AXIOM_WORKSPACES_DIR)
	docker compose --profile workers up -d --build --wait worker-dev-01 worker-dev-02 reviewer-rev-01 reviewer-sec-01 github-sync

workers-down:
	docker compose --profile workers stop model-proxy worker-dev-01 worker-dev-02 reviewer-rev-01 reviewer-sec-01 github-sync

# The daily report: plain numbers from the event log, no LLM.
report:
	docker compose exec -T api python -m axiom.report $(PROJECT)
