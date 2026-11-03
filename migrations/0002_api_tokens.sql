-- 0002_api_tokens: bearer tokens for the API. The actor comes from the token, not a header.
-- Only a SHA-256 hash of each token is stored; the token itself is shown once, at issue time.

CREATE TABLE api_tokens (
  id         bigserial PRIMARY KEY,
  token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  actor      text NOT NULL CHECK (actor ~ '^(human:[A-Za-z0-9._-]+|agent:[A-Z]+-[0-9]{2})$'),
  agent_id   bigint REFERENCES agents(id),
  label      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  CONSTRAINT agent_tokens_name_an_agent CHECK ((actor LIKE 'agent:%') = (agent_id IS NOT NULL))
);
CREATE INDEX api_tokens_active_idx ON api_tokens (token_hash) WHERE revoked_at IS NULL;
