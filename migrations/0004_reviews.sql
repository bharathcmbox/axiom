-- 0004: independent reviews (ADR-009). One review per required role per review round.
-- Review state machine: src/axiom/domain/review_machine.py, docs/state-machines.md.

CREATE TABLE reviews (
  id                bigserial PRIMARY KEY,
  task_id           bigint NOT NULL REFERENCES tasks(id),
  round             integer NOT NULL CHECK (round >= 1),
  role              text NOT NULL CHECK (role IN ('code_reviewer', 'security_reviewer')),
  status            text NOT NULL CHECK (status IN ('PENDING', 'CLAIMED', 'PASS', 'FAIL',
                                                    'ERROR', 'SUPERSEDED')),
  head_sha          text,                          -- the commit under review
  reviewer_agent_id bigint REFERENCES agents(id),  -- who claimed it last
  lease_expires_at  timestamptz,
  attempt_count     integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  verdict           text CHECK (verdict IN ('PASS', 'FAIL')),
  contract          jsonb,                         -- the validated review contract
  error             text,
  cost_usd          numeric(10, 4) CHECK (cost_usd >= 0),
  input_tokens      bigint CHECK (input_tokens >= 0),
  output_tokens     bigint CHECK (output_tokens >= 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz,
  CONSTRAINT one_review_per_role_per_round UNIQUE (task_id, round, role),
  CONSTRAINT claimed_iff_leased CHECK ((status = 'CLAIMED') = (lease_expires_at IS NOT NULL)),
  CONSTRAINT verdict_iff_decided CHECK ((status IN ('PASS', 'FAIL')) = (verdict IS NOT NULL)),
  CONSTRAINT verdict_matches_status CHECK (verdict IS NULL OR verdict = status)
);
CREATE INDEX reviews_open_idx ON reviews (role, created_at, id) WHERE status = 'PENDING';
CREATE INDEX reviews_leased_idx ON reviews (lease_expires_at) WHERE status = 'CLAIMED';

-- Review events reference the review; their task from/to states stay NULL so the task's
-- event chain (check_invariants) is unaffected.
ALTER TABLE events ADD COLUMN review_id bigint REFERENCES reviews(id);
