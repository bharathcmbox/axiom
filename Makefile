.PHONY: db up down logs test lint fmt migrate seed smoke

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
