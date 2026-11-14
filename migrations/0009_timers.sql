-- 0009: timers (ADR-018). Schedules a human sets up, and follow-ups Axiom sets for itself.
-- State machine: src/axiom/domain/timer_machine.py, docs/state-machines.md.

CREATE TABLE timers (
  id              bigserial PRIMARY KEY,
  kind            text NOT NULL CHECK (kind IN ('schedule', 'follow_up')),
  status          text NOT NULL CHECK (status IN ('PENDING', 'FIRED', 'CANCELLED')),
  project_id      bigint NOT NULL REFERENCES projects(id),
  due_at          timestamptz NOT NULL,
  every_seconds   integer CHECK (every_seconds IS NULL OR every_seconds >= 60),
  -- a schedule: the plan it requests each time
  specification   text,
  budget_usd      numeric(10, 4) CHECK (budget_usd > 0),
  -- a follow-up: what it watches, and the state that, once left, makes it moot
  task_id         bigint REFERENCES tasks(id),
  plan_id         bigint REFERENCES plans(id),
  reason          text,
  subject_state   text,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  fired_count     integer NOT NULL DEFAULT 0 CHECK (fired_count >= 0),
  last_fired_at   timestamptz,
  ended_by        text,
  ended_at        timestamptz,
  -- ADR-018: agents never create timers. Humans set schedules; Axiom's own code sets follow-ups.
  CONSTRAINT timer_not_from_an_agent CHECK (created_by NOT LIKE 'agent:%'),
  CONSTRAINT schedule_shape CHECK (kind <> 'schedule' OR (
    created_by LIKE 'human:%' AND every_seconds IS NOT NULL
    AND specification IS NOT NULL AND length(specification) > 0 AND budget_usd IS NOT NULL
    AND task_id IS NULL AND plan_id IS NULL)),
  CONSTRAINT follow_up_shape CHECK (kind <> 'follow_up' OR (
    created_by LIKE 'system:%' AND every_seconds IS NULL
    AND (task_id IS NULL) <> (plan_id IS NULL) AND reason IS NOT NULL AND subject_state IS NOT NULL)),
  CONSTRAINT ended_iff_not_pending CHECK ((status = 'PENDING') = (ended_at IS NULL))
);
CREATE INDEX timers_due_idx ON timers (due_at, id) WHERE status = 'PENDING';
-- One reminder per subject and reason, however often the subject comes back to that state.
CREATE UNIQUE INDEX one_pending_follow_up_per_task
  ON timers (task_id, reason) WHERE status = 'PENDING' AND task_id IS NOT NULL;
CREATE UNIQUE INDEX one_pending_follow_up_per_plan
  ON timers (plan_id, reason) WHERE status = 'PENDING' AND plan_id IS NOT NULL;

ALTER TABLE events ADD COLUMN timer_id bigint REFERENCES timers(id);
-- Which schedule asked for a plan, if any.
ALTER TABLE plans ADD COLUMN timer_id bigint REFERENCES timers(id);
