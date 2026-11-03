#!/usr/bin/env bash
# Prove that `docker compose up` works from a clean checkout of the current commit.
# Uses its own compose project and ports, so it does not touch a running dev stack.
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
work=$(mktemp -d)
project="axiom-smoke-$$"
export AXIOM_API_PORT=18000 AXIOM_DB_PORT=15432
api="http://127.0.0.1:${AXIOM_API_PORT}"
human=(-H "X-Axiom-Actor: human:smoke" -H "Content-Type: application/json")

cleanup() {
  (cd "$work/axiom" && docker compose -p "$project" down -v --remove-orphans) >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "==> cloning $(git -C "$repo" rev-parse --short HEAD) into a clean directory"
git clone -q "$repo" "$work/axiom"
cd "$work/axiom"

echo "==> docker compose up"
docker compose -p "$project" up -d --build --wait --quiet-pull 2>&1 | tail -4

echo "==> /healthz"
curl -fsS "$api/healthz"; echo
echo "==> seeded agents"
curl -fsS "$api/agents" | python3 -c 'import json,sys; print([a["key"] for a in json.load(sys.stdin)])'
echo "==> create project + task, claim it as DEV-01"
curl -fsS "${human[@]}" -d '{"key":"SMOKE","name":"Smoke test"}' "$api/projects" >/dev/null
curl -fsS "${human[@]}" -d '{"title":"hello","specification":"say hello"}' \
  "$api/projects/SMOKE/tasks" | python3 -c 'import json,sys; t=json.load(sys.stdin); print(t["key"], t["state"])'
curl -fsS -X POST -H "X-Axiom-Actor: agent:DEV-01" "$api/claims" \
  | python3 -c 'import json,sys; t=json.load(sys.stdin); print(t["key"], t["state"])'
echo "==> invariants"
curl -fsS "$api/invariants"; echo
echo "SMOKE OK"
