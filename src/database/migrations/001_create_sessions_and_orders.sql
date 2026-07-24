--- Migration 001: create base tables for sessions
--- Runs exactly once - tracked in the schema_migrations table

-- sessions: one row per lead. history holds the conversation; the remaining
-- columns hold the conversation state the AI reports in its action block
-- (written by bot/session.py::save_session, read by bot/handlers.py for the
-- pause check, the 1h timeout and context assembly).
--
-- The state lives in typed columns, not in a JSONB blob, so the funnel is
-- explorable with plain SQL (SELECT stage, qualification FROM sessions ...).
-- Consequence carried into the parser: any field the AI emits with no column
-- here is dropped silently.
--
-- No CHECK on stage/qualification on purpose: the allowed values live in Python
-- (session.valid_stages / valid_qualifications), the same single-source-of-truth
-- pattern as bookings.valid_booking_statuses, so widening an enum later is a
-- code change with no migration.
--
-- child_name mirrors trial_bookings.child_name: NULL means "not applicable",
-- distinct from an empty string. is_paused backs the handoff pause and MUST
-- survive an app restart, which is why it is a persisted column.
CREATE TABLE IF NOT EXISTS sessions (
    sender VARCHAR(20) PRIMARY KEY,
    history JSONB NOT NULL DEFAULT '[]',
    stage VARCHAR(30) NOT NULL DEFAULT 'greeting',
    lead_name VARCHAR(255) NULL,
    child_name VARCHAR(255) NULL,
    qualification VARCHAR(20) NOT NULL DEFAULT 'unknown',
    is_paused BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Speeds up funnel-style lookups by stage (e.g. finding paused/handoff sessions)
CREATE INDEX IF NOT EXISTS idx_sessions_stage
ON sessions(stage);
