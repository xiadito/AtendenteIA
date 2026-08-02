-- ============================================================
-- Migration 007: Create messages, the single source of truth for a conversation
--
-- Owner of writes: bot/messages.py — add_message() is called from
--                  bot/handlers.py (the lead's incoming text and the AI's
--                  outgoing reply, including while the conversation is paused)
--                  and from webhook/routes.py (the operator's reply typed in
--                  the dashboard inbox); mark_conversation_read() is called
--                  from the inbox routes when the operator opens a thread.
-- Owner of reads:  bot/handlers.py builds the LLM payload from the last N rows
--                  (get_recent_messages); webhook/routes.py renders the inbox
--                  list and the conversation screen (list_conversations /
--                  get_conversation).
--
-- This table replaces sessions.history (a JSONB blob capped at 10 turns, now
-- removed from migration 001). Two records of one conversation would drift, so
-- there is exactly one: every message any of the three authors produces lands
-- here, and the AI payload is a WINDOW over this table rather than a separate
-- store. That is what makes a human takeover possible at all — the operator
-- cannot answer a lead whose messages were never written down.
--
-- author is lead | ai | operator, validated in Python (messages.valid_authors)
-- with no CHECK here, the same single-source-of-truth pattern as
-- session.valid_stages and bookings.valid_booking_statuses: widening the set is
-- a code change with no migration. 'operator' messages are sent through the
-- gym's own Twilio number, so the lead sees one continuous thread and never
-- learns whether a human or the AI answered.
--
-- is_read only carries meaning for author = 'lead': it answers "has the
-- OPERATOR seen this?", which is what drives the unread badge in the inbox.
-- Messages the AI and the operator write are born read (is_read = TRUE) — an
-- outgoing message is nothing for the operator to catch up on.
--
-- id is BIGSERIAL, not SERIAL: this is by far the highest-volume table in the
-- project (every turn of every lead, forever), and it is the only place where
-- exhausting a 32-bit sequence is a realistic worry.
--
-- ON DELETE CASCADE is load-bearing, not decoration. bot/session.py::
-- clear_session() and the test suites' teardown both DELETE FROM sessions; with
-- a plain reference this new FK would break them with a foreign-key violation.
-- Cascading also matches the semantics: deleting a lead's session means
-- forgetting that lead, and a thread with no session is an orphan record of a
-- conversation nobody can continue.
--
-- ORDERING TRAP: never sort by created_at alone. The DEFAULT is NOW(), which in
-- Postgres is transaction_timestamp() — rows written inside one transaction all
-- get the SAME instant, so a lead's message and the AI's reply could come back
-- reversed. Every query orders by (created_at, id); the index below serves the
-- prefix and the PK breaks the tie.
-- ============================================================

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    sender VARCHAR(20) NOT NULL REFERENCES sessions(sender) ON DELETE CASCADE,
    author VARCHAR(20) NOT NULL,          -- lead | ai | operator (validated in Python)
    content TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Serves both hot reads: the operator opening a full thread, and the handler
-- taking the last N rows to build the LLM payload.
CREATE INDEX IF NOT EXISTS idx_messages_sender_created_at
ON messages (sender, created_at);

-- Partial index for the inbox's unread count. Only lead messages can be unread
-- (see is_read above), so the predicate keeps this index tiny — it holds the
-- backlog awaiting the operator, not the conversation archive.
CREATE INDEX IF NOT EXISTS idx_messages_unread_by_lead
ON messages (sender) WHERE author = 'lead' AND is_read = FALSE;
