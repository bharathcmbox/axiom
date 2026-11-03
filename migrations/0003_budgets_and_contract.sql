-- 0003: task contract fields used by workers, and project settings for the target repository.

ALTER TABLE tasks
  ADD COLUMN allowed_paths jsonb NOT NULL DEFAULT '[]'
    CHECK (jsonb_typeof(allowed_paths) = 'array');

ALTER TABLE projects
  ADD COLUMN default_branch text NOT NULL DEFAULT 'main',
  ADD COLUMN test_command   text;

-- Spend queries: per task, and per day.
CREATE INDEX executions_task_cost_idx ON executions (task_id) WHERE cost_usd IS NOT NULL;
CREATE INDEX executions_started_idx ON executions (started_at);
