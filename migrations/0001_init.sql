-- 0001_init: projects, agent registry, tasks, dependencies, executions, append-only events.
-- State machines: docs/state-machines.md. Never edit this file once applied; add a new migration.

CREATE TABLE projects (
  id            bigserial PRIMARY KEY,
  key           text NOT NULL UNIQUE CHECK (key ~ '^[A-Z][A-Z0-9]{1,9}$'),   -- 'AX'
  name          text NOT NULL,
  repo_url      text,
  specification text,
  settings      jsonb NOT NULL DEFAULT '{}',
  task_seq      integer NOT NULL DEFAULT 0 CHECK (task_seq >= 0),         -- AX-001, AX-002, ...
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agents (
  id                bigserial PRIMARY KEY,
  key               text NOT NULL UNIQUE CHECK (key ~ '^[A-Z]+-[0-9]{2}$'),  -- 'DEV-01'
  role              text NOT NULL CHECK (role IN ('engineering_manager', 'architect', 'developer',
                                                  'qa', 'security_reviewer', 'code_reviewer')),
  runtime           text NOT NULL CHECK (runtime IN ('claude_code', 'codex')),
  model             text,
  instructions_path text,
  max_concurrent    integer NOT NULL DEFAULT 1 CHECK (max_concurrent >= 1),
  enabled           boolean NOT NULL DEFAULT true,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
  id                  bigserial PRIMARY KEY,
  project_id          bigint NOT NULL REFERENCES projects(id),
  key                 text NOT NULL UNIQUE,
  title               text NOT NULL,
  specification       text NOT NULL,
  acceptance_criteria jsonb NOT NULL DEFAULT '[]'
                        CHECK (jsonb_typeof(acceptance_criteria) = 'array'),
  assigned_role       text NOT NULL DEFAULT 'developer'
                        CHECK (assigned_role IN ('engineering_manager', 'architect', 'developer',
                                                 'qa', 'security_reviewer', 'code_reviewer')),
  priority            text NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
  state               text NOT NULL CHECK (state IN ('PENDING', 'READY', 'ASSIGNED', 'IN_PROGRESS',
                        'WAITING_FOR_HUMAN', 'IN_REVIEW', 'AWAITING_MERGE', 'DONE', 'FAILED',
                        'CANCELLED')),
  attempt_count       integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_retries         integer NOT NULL DEFAULT 2 CHECK (max_retries >= 0),
  review_rounds       integer NOT NULL DEFAULT 0 CHECK (review_rounds >= 0),
  budget_usd          numeric(10, 2) CHECK (budget_usd >= 0),          -- enforced from Phase 2
  branch              text,                                            -- filled from Phase 2
  pull_request_url    text,                                            -- filled from Phase 2
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tasks_project_idx ON tasks (project_id);
CREATE INDEX tasks_ready_idx ON tasks (priority, created_at, id) WHERE state = 'READY';

CREATE TABLE task_dependencies (
  task_id       bigint NOT NULL REFERENCES tasks(id),
  depends_on_id bigint NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on_id),
  CHECK (task_id <> depends_on_id)
);
CREATE INDEX task_dependencies_depends_on_idx ON task_dependencies (depends_on_id);

CREATE TABLE executions (
  id               bigserial PRIMARY KEY,
  task_id          bigint NOT NULL REFERENCES tasks(id),
  agent_id         bigint NOT NULL REFERENCES agents(id),
  attempt          integer NOT NULL CHECK (attempt >= 1),
  status           text NOT NULL CHECK (status IN ('STARTING', 'RUNNING', 'COMPLETED', 'FAILED',
                                                   'TIMED_OUT', 'CANCELLED')),
  lease_expires_at timestamptz,
  started_at       timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz,
  result           jsonb,
  error            text,
  cost_usd         numeric(10, 4) CHECK (cost_usd >= 0),
  input_tokens     bigint CHECK (input_tokens >= 0),
  output_tokens    bigint CHECK (output_tokens >= 0),
  transcript_uri   text,
  CONSTRAINT active_iff_leased  CHECK ((status IN ('STARTING', 'RUNNING')) = (lease_expires_at IS NOT NULL)),
  CONSTRAINT active_iff_not_ended CHECK ((status IN ('STARTING', 'RUNNING')) = (ended_at IS NULL))
);
CREATE UNIQUE INDEX one_active_execution_per_task ON executions (task_id)
  WHERE status IN ('STARTING', 'RUNNING');
CREATE INDEX executions_agent_active_idx ON executions (agent_id)
  WHERE status IN ('STARTING', 'RUNNING');
CREATE INDEX executions_lease_idx ON executions (lease_expires_at)
  WHERE status IN ('STARTING', 'RUNNING');

CREATE TABLE events (
  id           bigserial PRIMARY KEY,           -- ordering key; occurred_at is per transaction
  occurred_at  timestamptz NOT NULL DEFAULT now(),
  actor        text NOT NULL,                   -- 'human:…' | 'agent:DEV-01' | 'system:reaper'
  type         text NOT NULL,                   -- e.g. TASK_ASSIGNED
  project_id   bigint REFERENCES projects(id),
  task_id      bigint REFERENCES tasks(id),
  execution_id bigint REFERENCES executions(id),
  agent_id     bigint REFERENCES agents(id),
  from_state   text,
  to_state     text,
  payload      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX events_task_idx ON events (task_id, id);

CREATE FUNCTION events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'events is append-only (% rejected)', TG_OP;
END
$$;

CREATE TRIGGER events_no_update_delete BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION events_append_only();
