# API

Interactive docs (OpenAPI) are served at <http://127.0.0.1:8000/docs> when the stack is up.

Every request except `GET /healthz` needs a bearer token: `Authorization: Bearer axm_…`. The
token decides who you are (`human:<name>` or `agent:<AGENT-KEY>`); clients cannot choose their
actor. `system:` actors (the reaper, dependency promotion) exist only inside the control plane
and cannot hold tokens. Issue tokens where the database is reachable:

```bash
docker compose exec api python -m axiom.registry.tokens issue human:bharath   # printed once
docker compose exec api python -m axiom.registry.tokens issue agent:DEV-01
docker compose exec api python -m axiom.registry.tokens list
docker compose exec api python -m axiom.registry.tokens revoke <id>
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness + database check |
| GET | `/invariants` | Cross-table invariant violations (expect `[]`) |
| POST / GET | `/projects`, `/projects/{key}` | Create / list / get projects |
| POST / GET | `/agents`, `/agents/{key}` | Register / list (`?role=&enabled=`) / get agents |
| POST | `/agents/{key}/enable`, `/agents/{key}/disable` | Toggle an agent |
| POST | `/projects/{key}/tasks` | Create a task (the task contract); returns PENDING or READY |
| GET | `/tasks?project=&state=`, `/tasks/{key}` | List / get tasks |
| POST | `/tasks/{key}/commands/{command}` | Apply a command (body: `agent_key`, `payload`, `lease_seconds`) |
| POST | `/claims` | Agent claims next READY task for its role (200 task, 204 none) |
| POST | `/tasks/{key}/heartbeat` | Owning agent extends its lease (no event) |
| GET | `/tasks/{key}/executions` | Execution history of a task |
| GET | `/tasks/{key}/events`, `/events?after_id=&limit=` | Event log |

Commands accepted on `/tasks/{key}/commands/{command}`: `assign`, `start`, `submit`,
`ask_human`, `answer`, `fail`, `approve`, `request_changes`, `reject_merge`,
`merge`, `retry`, `cancel`. (`claim` is `POST /claims`; `promote` and `time_out` are internal.) See
[state-machines.md](state-machines.md) for which actor may issue each one, and from which
states.

## Errors

Body: `{"error": "<code>", "detail": "<message>", ...}`.

| Status | Codes |
|---|---|
| 401 | missing, invalid or revoked bearer token |
| 403 | `actor_not_permitted`, `not_execution_owner`, `forbidden` (e.g. an agent changing the registry) |
| 404 | `not_found` |
| 409 | `illegal_transition` (adds `state`, `command`, `allowed`), `assignment_rejected`, `dependencies_not_met`, `lease_expired`, `lease_not_expired`, `no_active_execution`, `already_exists` |
| 422 | request validation, `invalid_input` |

## Walkthrough: one task from creation to DONE by hand

```bash
api=http://127.0.0.1:8000
HTOK=$(docker compose exec -T api python -m axiom.registry.tokens issue human:bharath)
DTOK=$(docker compose exec -T api python -m axiom.registry.tokens issue agent:DEV-01)
H=(-H "Authorization: Bearer $HTOK" -H "Content-Type: application/json")
DEV=(-H "Authorization: Bearer $DTOK" -H "Content-Type: application/json")

curl -s "${H[@]}" -d '{"key":"AX","name":"Axiom"}' $api/projects
curl -s "${H[@]}" $api/projects/AX/tasks -d '{
  "title": "Implement POST /alerts",
  "specification": "Validate and persist alerts",
  "acceptance_criteria": ["Rejects malformed payloads with 400", "Persists valid alerts"],
  "priority": "P1", "budget_usd": 3.00
}'                                                          # -> AX-001, READY

curl -s -X POST "${DEV[@]}" $api/claims                    # -> ASSIGNED to DEV-01
curl -s "${DEV[@]}" -d '{}' $api/tasks/AX-001/commands/start
curl -s "${DEV[@]}" -d '{}' $api/tasks/AX-001/heartbeat
curl -s "${DEV[@]}" $api/tasks/AX-001/commands/submit \
     -d '{"payload": {"summary": "done", "files_changed": ["src/alerts/api.py"]}}'
curl -s "${H[@]}" -d '{}' $api/tasks/AX-001/commands/approve
curl -s "${H[@]}" -d '{}' $api/tasks/AX-001/commands/merge   # -> DONE

curl -s "${H[@]}" -d '{}' $api/tasks/AX-001/commands/merge   # -> 409 illegal_transition
curl -s "${H[@]}" $api/tasks/AX-001/events                     # full history
```
