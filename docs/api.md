# API

Interactive docs (OpenAPI) are served at <http://127.0.0.1:8000/docs> when the stack is up.

Every write request needs an `X-Axiom-Actor` header: `human:<name>`, `agent:<AGENT-KEY>` or
`system:<name>`. In Phase 1 it is trusted, not authenticated; the API listens on localhost only.

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
`ask_human`, `answer`, `fail`, `time_out`, `approve`, `request_changes`, `reject_merge`,
`merge`, `retry`, `cancel`. (`claim` is `POST /claims`; `promote` is internal.) See
[state-machines.md](state-machines.md) for which actor may issue each one, and from which
states.

## Errors

Body: `{"error": "<code>", "detail": "<message>", ...}`.

| Status | Codes |
|---|---|
| 400 | malformed `X-Axiom-Actor` |
| 403 | `actor_not_permitted`, `not_execution_owner`, `forbidden` |
| 404 | `not_found` |
| 409 | `illegal_transition` (adds `state`, `command`, `allowed`), `assignment_rejected`, `dependencies_not_met`, `lease_expired`, `lease_not_expired`, `no_active_execution`, `already_exists` |
| 422 | request validation, `invalid_input` |

## Walkthrough: one task from creation to DONE by hand

```bash
api=http://127.0.0.1:8000
H=(-H "X-Axiom-Actor: human:bharath" -H "Content-Type: application/json")
DEV=(-H "X-Axiom-Actor: agent:DEV-01" -H "Content-Type: application/json")

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
curl -s $api/tasks/AX-001/events                              # full history
```
