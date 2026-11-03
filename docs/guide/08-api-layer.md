# 8. The API layer

The REST API is deliberately **thin**. Routes parse the request, call one service function and
serialize the result. No business rule lives in [`api/`](../../src/axiom/api/).

## The path of one request

```mermaid
sequenceDiagram
    autonumber
    participant C as curl
    participant F as FastAPI
    participant D as Dependencies
    participant R as Route
    participant S as Service (orchestrator)
    participant H as Error handlers
    C->>F: POST /tasks/AX-001/commands/submit<br/>X-Axiom-Actor: agent:DEV-01<br/>{"payload": {...}}
    F->>D: get_actor(header) → Actor(AGENT, "DEV-01")
    F->>D: get_conn() → borrow a connection from the pool
    F->>F: validate body as CommandIn (pydantic)
    F->>R: command_route(key, command, conn, actor, body)
    R->>R: command name → TaskCommand (422 if unknown or internal)
    R->>S: apply_command(conn, actor, "AX-001", SUBMIT, payload=...)
    alt legal
        S-->>R: Task
        R-->>C: 200 TaskOut JSON
    else domain error
        S--xH: raises e.g. LeaseExpired
        H-->>C: 409 {"error": "lease_expired", "detail": "..."}
    end
    F->>D: return connection to the pool
```

## Where things live

```mermaid
flowchart LR
    subgraph api["src/axiom/api/"]
        app["app.py<br/>create_app(): lifespan,<br/>pool, reaper, routers"]
        routes["routes.py<br/>endpoints"]
        schemas["schemas.py<br/>pydantic in/out models<br/>(TaskIn = the task contract)"]
        errors["errors.py<br/>domain error → HTTP status"]
    end
    app --> routes
    app --> errors
    routes --> schemas
```

## The actor header

Every write needs `X-Axiom-Actor`. It is parsed into an `Actor(kind, name)`:

```mermaid
flowchart LR
    h["X-Axiom-Actor: agent:DEV-01"] --> p{"Actor.parse"}
    p -->|"missing"| e422["422 (FastAPI: required header)"]
    p -->|"no colon / unknown kind"| e400["400 malformed actor"]
    p -->|ok| a["Actor(kind=AGENT, name='DEV-01')"]
    a --> rules["the transition table checks the kind;<br/>execution guards check the name"]
```

> ⚠️ **Phase 1 trusts this header.** Anyone who can reach the API can claim to be `human:` or
> `system:`. That is acceptable only because the API listens on 127.0.0.1. Phase 2 replaces it
> with per-agent tokens before any worker runs unattended.

## Error mapping

Domain code raises typed exceptions (`axiom/errors.py` and `domain/task_machine.py`). Exactly
one module, `api/errors.py`, turns them into HTTP responses:

```mermaid
flowchart LR
    subgraph s404["404"]
        NotFound
    end
    subgraph s403["403: you are not allowed"]
        ActorNotPermitted
        NotExecutionOwner
        Forbidden
    end
    subgraph s409["409: not possible in the current state"]
        IllegalTransition
        AssignmentRejected
        DependenciesNotMet
        LeaseExpired
        LeaseNotExpired
        NoActiveExecution
        AlreadyExists
    end
    subgraph s422["422: bad input"]
        InvalidInput
        validation["pydantic validation"]
    end
```

The error code in the body is derived from the class name (`IllegalTransition` →
`illegal_transition`). An illegal transition also says what *would* be legal:

```json
{
  "error": "illegal_transition",
  "detail": "cannot merge a task in READY; allowed: claim, assign, cancel",
  "state": "READY",
  "command": "merge",
  "allowed": ["claim", "assign", "cancel"]
}
```

## Endpoint map

```mermaid
flowchart TB
    root(("API"))
    root --> ops["ops"]
    ops --> hz["GET /healthz"]
    ops --> iv["GET /invariants"]
    root --> pj["projects"]
    pj --> p1["POST /projects"]
    pj --> p2["GET /projects, /projects/{key}"]
    root --> ag["agents"]
    ag --> a1["POST /agents"]
    ag --> a2["GET /agents?role=&enabled="]
    ag --> a3["POST /agents/{key}/enable | disable"]
    root --> tk["tasks"]
    tk --> t1["POST /projects/{key}/tasks"]
    tk --> t2["GET /tasks?project=&state=, /tasks/{key}"]
    tk --> t3["POST /tasks/{key}/commands/{command}"]
    tk --> t4["POST /claims"]
    tk --> t5["POST /tasks/{key}/heartbeat"]
    tk --> t6["GET /tasks/{key}/executions"]
    root --> ev["events"]
    ev --> e1["GET /tasks/{key}/events"]
    ev --> e2["GET /events?after_id=&limit="]
```

Full reference: [api.md](../api.md). Interactive: <http://127.0.0.1:8000/docs>.

## Sync code in an async framework

FastAPI is async, but Axiom's routes are plain `def` functions using a **synchronous** psycopg
pool. FastAPI runs `def` routes in a thread pool, so a slow query blocks only its own thread.
That kept the code simple: no `async`/`await` around every database call, and locking
behaviour that is easy to read. At Axiom's scale (a handful of agents), that is the right
trade-off.

## Try it

1. Open <http://127.0.0.1:8000/docs>, expand `POST /tasks/{key}/commands/{command}`, and send
   an illegal command. Read the 409 body.
2. Send a request with `X-Axiom-Actor: robot:x`. You should get a 400.
3. Find `STATUS` in `api/errors.py`. Add a new error class to `axiom/errors.py` without
   mapping it, and work out what FastAPI would return. (Answer: 500, which is why every domain
   error must be listed.)
