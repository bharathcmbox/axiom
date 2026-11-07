-- 0008: plans (ADR-011). A human's spec, a planner's proposal, and a human's decision.
-- State machine: src/axiom/domain/plan_machine.py, docs/state-machines.md.

CREATE TABLE plans (
  id                bigserial PRIMARY KEY,
  project_id        bigint NOT NULL REFERENCES projects(id),
  specification     text NOT NULL CHECK (length(specification) > 0),
  budget_usd        numeric(10, 4) NOT NULL CHECK (budget_usd > 0),  -- the most its tasks may total
  status            text NOT NULL CHECK (status IN ('REQUESTED', 'CLAIMED', 'PROPOSED', 'APPROVED',
                                                    'ERROR', 'CANCELLED')),
  planner_agent_id  bigint REFERENCES agents(id),   -- who claimed it last
  lease_expires_at  timestamptz,
  attempt_count     integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  feedback          jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(feedback) = 'array'),
  contract          jsonb,                          -- the validated proposal
  error             text,
  cost_usd          numeric(10, 4) CHECK (cost_usd >= 0),
  cost_estimated    boolean NOT NULL DEFAULT false,
  run_budget_usd    numeric(10, 4) CHECK (run_budget_usd >= 0),
  created_by        text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  decided_by        text,                           -- the human who approved or cancelled it
  decided_at        timestamptz,
  CONSTRAINT plan_claimed_iff_leased CHECK ((status = 'CLAIMED') = (lease_expires_at IS NOT NULL)),
  CONSTRAINT plan_proposal_has_contract
    CHECK (status NOT IN ('PROPOSED', 'APPROVED') OR contract IS NOT NULL),
  CONSTRAINT plan_approval_has_a_human CHECK (status <> 'APPROVED' OR decided_by LIKE 'human:%')
);
CREATE INDEX plans_requested_idx ON plans (created_at, id) WHERE status = 'REQUESTED';

ALTER TABLE events ADD COLUMN plan_id bigint REFERENCES plans(id);

-- A task created by approving a plan remembers which plan, and which entry in it.
ALTER TABLE tasks
  ADD COLUMN plan_id bigint REFERENCES plans(id),
  ADD COLUMN plan_ref text,
  ADD CONSTRAINT one_task_per_plan_ref UNIQUE (plan_id, plan_ref);

-- What a QA task may touch: a project's tests, and nothing else (SPEC).
ALTER TABLE projects
  ADD COLUMN test_paths jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(test_paths) = 'array');
