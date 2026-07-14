-- D1 (SQLite) schema for the AI call screener.
-- Canonical truth is the stored call records; this DB is a rebuildable index.
-- Apply locally:  npm run db:apply:local
-- Apply remote:   npm run db:apply:remote

-- Allowlist: known contacts that ring straight through, never screened.
CREATE TABLE IF NOT EXISTS contacts (
  number_e164 TEXT PRIMARY KEY,
  name        TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT 'manual',
  created_at  TEXT NOT NULL
);

-- Learning blocklist: numbers we reject for $0 (before answering).
CREATE TABLE IF NOT EXISTS blocklist (
  number_e164 TEXT PRIMARY KEY,
  reason      TEXT NOT NULL,              -- gate_fail | claude_spam | manual | reputation
  confidence  REAL NOT NULL DEFAULT 1.0,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  hit_count   INTEGER NOT NULL DEFAULT 1
);

-- One row per inbound call, with the verdict and an estimated cost.
CREATE TABLE IF NOT EXISTS calls (
  id                TEXT PRIMARY KEY,
  from_e164         TEXT NOT NULL DEFAULT '',
  to_e164           TEXT NOT NULL DEFAULT '',
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  outcome_stage     TEXT NOT NULL,        -- allowlist | blocklist | gate_fail | conversation
  verdict           TEXT NOT NULL,        -- allowed | bridged | message | spam | gate_fail | rejected
  caller_name       TEXT,
  reason            TEXT,
  transcript        TEXT,
  cost_estimate_usd REAL NOT NULL DEFAULT 0,
  recording_url     TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_started_at ON calls (started_at);
CREATE INDEX IF NOT EXISTS idx_calls_from ON calls (from_e164);

-- Single-row operator settings (overrides env defaults).
CREATE TABLE IF NOT EXISTS settings (
  id                        INTEGER PRIMARY KEY CHECK (id = 1),
  user_cell_e164            TEXT,
  gate_prompt               TEXT,
  post_gate_action          TEXT,
  reputation_lookup_enabled INTEGER,
  daily_budget_usd          REAL,
  quiet_hours               TEXT,
  record_calls              INTEGER NOT NULL DEFAULT 0
);
