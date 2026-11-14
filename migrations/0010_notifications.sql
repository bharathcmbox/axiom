-- 0010: notifications (ADR-019). An outbox: one row per thing a human should hear about, written in
-- the same transaction as its cause, delivered later by the sync service.

CREATE TABLE notifications (
  id              bigserial PRIMARY KEY,
  project_id      bigint NOT NULL REFERENCES projects(id),
  reason          text NOT NULL CHECK (reason IN ('credential_exposure', 'question', 'escalated',
                                                  'pr_ready', 'plan_ready', 'reminder',
                                                  'schedule_refused')),
  task_id         bigint REFERENCES tasks(id),
  plan_id         bigint REFERENCES plans(id),
  timer_id        bigint REFERENCES timers(id),
  dedup_key       text NOT NULL UNIQUE,
  push            boolean NOT NULL,          -- delivered outside the dashboard (quiet by default)
  created_at      timestamptz NOT NULL DEFAULT now(),
  delivered_at    timestamptz,
  attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  delivery_error  text,
  CONSTRAINT delivered_only_if_pushed CHECK (push OR delivered_at IS NULL)
);
CREATE INDEX notifications_undelivered_idx ON notifications (id) WHERE push AND delivered_at IS NULL;

-- The issue in the project's repository that notifications are posted to, once the sync has opened it.
ALTER TABLE projects ADD COLUMN inbox_issue integer CHECK (inbox_issue > 0);
