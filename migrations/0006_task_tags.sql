-- 0006: task tags, e.g. "demo" for planted or smoke-test work that metrics exclude by default.
ALTER TABLE tasks ADD COLUMN tags jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(tags) = 'array');
