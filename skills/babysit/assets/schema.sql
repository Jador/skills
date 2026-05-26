-- Babysit SQLite schema
-- Idempotent: safe to apply repeatedly against the same database.
-- Applied to: ${CLAUDE_PLUGIN_DATA}/babysit/state.db

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS seen_events (
    pr INT,
    kind TEXT,
    event_id TEXT,
    ts TEXT,
    PRIMARY KEY (pr, kind, event_id)
);

CREATE TABLE IF NOT EXISTS pipelines (
    repo TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    ts TEXT
);
