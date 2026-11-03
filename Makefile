.PHONY: db up down logs test test-docker test-live sandbox-image lint fmt migrate seed smoke demo-restart

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
