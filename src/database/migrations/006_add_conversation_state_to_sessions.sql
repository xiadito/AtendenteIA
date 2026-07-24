-- ============================================================
-- Migration 006: Add discrete conversation state to sessions
--
-- Owner of writes: bot/session.py::save_session(), driven by the action block
--                  the AI returns each turn (parsed in bot/handlers.py).
-- Owner of reads:  bot/handlers.py (pause check, 1h timeout, context assembly)
--                  and bot/session.py::get_session()/get_all_sessions().
--
-- Module 3 turns the session into the concrete source of truth for where the
-- lead is in the funnel. These are typed columns, not a JSONB blob, so the
-- state is explorable with plain SQL (SELECT stage, qualification FROM ...).
-- Consequence carried into the parser: any field the AI emits that has no
-- column here is dropped silently.
--
-- No CHECK constraint on stage/qualification on purpose: the allowed values
-- live in Python (session.valid_stages / session.valid_qualifications), the
-- same single-source-of-truth pattern as bookings.valid_booking_statuses, so
-- widening an enum later is a code change with no migration.
--
-- child_name mirrors trial_bookings.child_name (migration 005): NULL = not
-- applicable, distinct from an empty string. is_paused backs the handoff pause
-- (Module 3) and MUST survive an app restart, which is why it is a persisted
-- column and not in-memory state.
-- ============================================================

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS stage         VARCHAR(30)  NOT NULL DEFAULT 'greeting',
    ADD COLUMN IF NOT EXISTS lead_name     VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS child_name    VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS qualification VARCHAR(20)  NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS is_paused     BOOLEAN      NOT NULL DEFAULT FALSE;

-- Speeds up funnel-style lookups by stage (e.g. finding paused/handoff sessions)
CREATE INDEX IF NOT EXISTS idx_sessions_stage
ON sessions (stage);
