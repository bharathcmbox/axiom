-- Phase 4 T3.
-- cost_estimated: the cost was computed from a transcript because the run never reported one.
-- run_budget_usd: what an active run may spend. Costs are only known when a run ends, so later
--   claims subtract these reservations; a lock alone could not stop two runs overspending.
-- python_version: passed to uv in the sandbox, so verified tests match the project's CI.
ALTER TABLE executions
  ADD COLUMN cost_estimated boolean NOT NULL DEFAULT false,
  ADD COLUMN run_budget_usd numeric(10, 4) CHECK (run_budget_usd >= 0);
ALTER TABLE reviews
  ADD COLUMN cost_estimated boolean NOT NULL DEFAULT false,
  ADD COLUMN run_budget_usd numeric(10, 4) CHECK (run_budget_usd >= 0);
ALTER TABLE projects
  ADD COLUMN python_version text CHECK (python_version ~ '^3\.[0-9]+$');
