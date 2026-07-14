CREATE TABLE IF NOT EXISTS usage (
  event_id     TEXT PRIMARY KEY,
  response_id  TEXT,
  ts           TIMESTAMPTZ NOT NULL,
  project      TEXT,
  commit_sha   TEXT,
  session_id   TEXT,
  model_asked  TEXT NOT NULL,
  model        TEXT,
  mode         TEXT NOT NULL DEFAULT 'chat',
  via          TEXT,
  cached       BOOLEAN NOT NULL DEFAULT FALSE,
  input_tokens  INTEGER, output_tokens INTEGER, cache_tokens INTEGER,
  cost_usd     NUMERIC(12,6) NOT NULL DEFAULT 0,
  latency_s    NUMERIC,
  raw          JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_ts_idx      ON usage (ts);
CREATE INDEX IF NOT EXISTS usage_project_idx ON usage (project, ts);
