--- Migration 001: create base tables for sessions
--- Runs exactly once - tracked in the schema_migrations table

-- sessions: one row per lead. It holds the conversation STATE the AI reports in
-- its action block (written by bot/session.py::save_session, read by
-- bot/handlers.py for the pause check, the 1h timeout and context assembly).
--
-- It no longer holds the conversation itself. The messages live in their own
-- table (migration 007), which is the single source of truth for what was said:
-- the operator inbox reads the whole thread from there, and bot/handlers.py
-- builds the LLM payload from the last N rows of it. The old `history` JSONB
-- column was capped at 10 turns and invisible to anything but the AI, so it was
-- retired rather than kept in parallel — two records of the same conversation
-- would inevitably drift.
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
-- survive an app restart, which is why it is a persisted column. Since the
-- operator inbox exists, is_paused is also the takeover switch: TRUE means a
-- human holds the conversation and the AI stays silent, and the inbox's
-- "devolver para a IA" button is the only thing that ever clears it.
--
-- Two transient markers back the takeover hand-back and the inactivity reset:
--
--   needs_resume_note — set TRUE when the operator gives the conversation back
--   to the AI. The next prompt assembly injects a short note telling the model a
--   human just handed the thread over, then clears the flag, so the note is
--   delivered exactly once and never leaks into later turns.
--
--   conversation_started_at — the boundary of the CURRENT conversation. The 1h
--   inactivity timeout stamps NOW() here instead of deleting anything: the LLM
--   payload only reads messages created at or after this instant (so a timed-out
--   lead really does get a fresh start), while the inbox keeps showing the full
--   thread, which is exactly what the operator needs in order to catch up.
CREATE TABLE IF NOT EXISTS sessions (
    sender VARCHAR(20) PRIMARY KEY,
    stage VARCHAR(30) NOT NULL DEFAULT 'greeting',
    lead_name VARCHAR(255) NULL,
    child_name VARCHAR(255) NULL,
    qualification VARCHAR(20) NOT NULL DEFAULT 'unknown',
    is_paused BOOLEAN NOT NULL DEFAULT FALSE,
    needs_resume_note BOOLEAN NOT NULL DEFAULT FALSE,
    conversation_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Speeds up funnel-style lookups by stage (e.g. finding paused/handoff sessions)
CREATE INDEX IF NOT EXISTS idx_sessions_stage
ON sessions(stage);
