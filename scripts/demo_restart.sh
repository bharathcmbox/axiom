#!/usr/bin/env bash
# Durability demo: put tasks into many states, SIGKILL the API *and* Postgres, start them
# again, and show that every task, execution and event is unchanged. Then show that the
# in-flight execution, whose worker never comes back, is timed out by the reaper.
#
# Usage: scripts/demo_restart.sh   (expects the stack from `make up`; API on :8000)
set -euo pipefail

api="http://127.0.0.1:${AXIOM_API_PORT:-8000}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
issue() { docker compose exec -T api python -m axiom.registry.tokens issue "$1"; }
HTOK=$(issue human:demo)
export HTOK
H=(-H "Authorization: Bearer $HTOK" -H "Content-Type: application/json")
DEV1=(-H "Authorization: Bearer $(issue agent:DEV-01)" -H "Content-Type: application/json")
DEV2=(-H "Authorization: Bearer $(issue agent:DEV-02)" -H "Content-Type: application/json")
get() { curl -fsS -H "Authorization: Bearer $HTOK" "$@"; }
LEASE=${DEMO_LEASE_SECONDS:-30}

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
post() { curl -sS --fail-with-body -X POST "$@"; }
cmd() { # cmd KEY COMMAND ACTOR_ARRAY_NAME [JSON_BODY]
  local -n actor=$3
  post "${actor[@]}" -d "${4:-{\}}" "$api/tasks/$1/commands/$2" >/dev/null
}
task() { # creates a task tagged demo: this script's tasks must not count as delivery (ADR-012)
  local key
  key=$(post "${H[@]}" -d "$2" "$api/projects/$1/tasks" | json 'd["key"]')
  post "${H[@]}" -d '{"tags":["demo"]}' "$api/tasks/$key/tags" >/dev/null
  echo "$key"
}

show_tasks() { python3 -c '
import json, sys
for t in json.load(sys.stdin):
    print("   {:<14} {}".format(t["key"], t["state"]))'
}
show_events() { python3 -c '
import json, sys
for e in json.load(sys.stdin):
    print("   {:<20} {:<12} -> {:<12} by {}".format(
        e["type"], str(e["from_state"]), e["to_state"] or "", e["actor"]))'
}

wait_healthy() {
  for _ in $(seq 1 60); do curl -fsS "$api/healthz" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "API did not become healthy" >&2; return 1
}

snapshot() { # snapshot PROJECT FILE
  python3 - "$api" "$1" > "$2" <<'PY'
import json, sys, urllib.request
api, project = sys.argv[1], sys.argv[2]
import os
req = lambda path: urllib.request.Request(api + path, headers={"Authorization": "Bearer " + os.environ["HTOK"]})
get = lambda path: json.load(urllib.request.urlopen(req(path)))
tasks = get(f"/tasks?project={project}")
pid = get(f"/projects/{project}")["id"]
events, after = [], 0
while batch := get(f"/events?after_id={after}&limit=1000"):
    events += [e for e in batch if e["project_id"] == pid]
    after = batch[-1]["id"]
snap = {"tasks": tasks,
        "executions": {t["key"]: get(f"/tasks/{t['key']}/executions") for t in tasks},
        "events": events}
print(json.dumps(snap, indent=1, sort_keys=True))
PY
}

wait_healthy
echo "==> waiting until DEV-01 and DEV-02 are free (e.g. leases left over from an earlier run)"
for _ in $(seq 1 120); do
  busy=$(get "$api/tasks?state=ASSIGNED" "$api/tasks?state=IN_PROGRESS" 2>/dev/null \
         | grep -c '"assigned_role":"developer"' || true)
  [ "$busy" = "0" ] && break; sleep 1
done
P="D$(date +%H%M%S)"
echo "==> project $P"
post "${H[@]}" -d "{\"key\":\"$P\",\"name\":\"Restart demo\"}" "$api/projects" >/dev/null

ready=$(task "$P" '{"title":"ready","specification":"s"}')
pending=$(task "$P" "{\"title\":\"pending\",\"specification\":\"s\",\"dependencies\":[\"$ready\"]}")
cancelled=$(task "$P" '{"title":"cancelled","specification":"s"}'); cmd "$cancelled" cancel H

drive() { # drive TITLE STEPS... (DEV-02 does the agent steps)
  local key; key=$(task "$P" "{\"title\":\"$1\",\"specification\":\"s\"}"); shift
  for step in "$@"; do
    case $step in
      assign) cmd "$key" assign H '{"agent_key":"DEV-02"}' ;;
      start|submit) cmd "$key" "$step" DEV2 ;;
      ask_human) cmd "$key" ask_human DEV2 '{"payload":{"question":"REST or gRPC?"}}' ;;
      *) cmd "$key" "$step" H ;;
    esac
  done
  echo "$key"
}
done_=$(drive done assign start submit approve merge)
merge_=$(drive awaiting_merge assign start submit approve)
review=$(drive in_review assign start submit)
waiting=$(drive waiting assign start ask_human)
failed=$(task "$P" '{"title":"failed","specification":"s","max_retries":0}')
cmd "$failed" assign H '{"agent_key":"DEV-02"}'; cmd "$failed" start DEV2
cmd "$failed" fail DEV2 '{"payload":{"error":"tests failed"}}'
inflight=$(task "$P" '{"title":"in flight","specification":"s"}')
cmd "$inflight" assign H "{\"agent_key\":\"DEV-01\",\"lease_seconds\":$LEASE}"
cmd "$inflight" start DEV1 "{\"lease_seconds\":$LEASE}"

echo "==> tasks before the crash"
get "$api/tasks?project=$P" | show_tasks
snapshot "$P" "$work/before.json"
echo "    ($(json 'len(d["events"])' < "$work/before.json") events, in-flight task $inflight held by DEV-01 with a ${LEASE}s lease)"

echo "==> SIGKILL api and db"
docker compose kill -s SIGKILL api db
docker compose ps --all --format '   {{.Service}}: {{.State}} {{.Status}}'

echo "==> start again"
docker compose up -d --wait 2>&1 | grep -E "Started|Healthy" | sed 's/^/   /' || true
wait_healthy
snapshot "$P" "$work/after.json"

echo "==> compare state before and after the crash"
if diff -u "$work/before.json" "$work/after.json"; then
  echo "   IDENTICAL: $(json 'len(d["tasks"])' < "$work/after.json") tasks, executions and $(json 'len(d["events"])' < "$work/after.json") events survived"
else
  echo "   DIFFERENT (see diff above)"; exit 1
fi

echo "==> DEV-01 never comes back; waiting for its lease to expire and the reaper to act"
for _ in $(seq 1 $((LEASE + 30))); do
  state=$(get "$api/tasks/$inflight" | json 'd["state"]')
  [ "$state" = "READY" ] && break; sleep 1
done
get "$api/tasks/$inflight/events" | show_events
get "$api/tasks/$inflight" | json '"   {} is {} with attempt_count={}".format(d["key"], d["state"], d["attempt_count"])'
echo "==> invariants: $(get "$api/invariants")"
