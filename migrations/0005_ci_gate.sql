-- 0005: CI gate inputs (ADR-010). The GitHub sync writes the CI conclusion for the PR head.

ALTER TABLE projects
  ADD COLUMN require_ci boolean NOT NULL DEFAULT false;

ALTER TABLE tasks
  ADD COLUMN head_sha   text,     -- commit of the latest submitted work (what reviews and CI judge)
  ADD COLUMN ci_status  text CHECK (ci_status IN ('pending', 'success', 'failure')),
  ADD COLUMN ci_sha     text,     -- commit the CI status belongs to
  ADD COLUMN ci_details jsonb;
