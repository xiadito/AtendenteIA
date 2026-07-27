-- ============================================================
-- Migration 006: Create owner_notifications table for owner notifications
--
-- Owner of writes: bot/owner_notifications.py — enqueue_notification() is
--                  called from bot/handlers.py right after a booking or a
--                  handoff is persisted; mark_sent()/mark_attempt_failed()
--                  are called from jobs/drain_notifications.py (the Railway
--                  cron service); register_owner_response() is called from
--                  webhook/routes.py::receive_twilio_owner().
-- Owner of reads:  jobs/drain_notifications.py (drains pending rows every
--                  cron cycle); webhook/routes.py, indirectly, via
--                  integrations/store.py::get_owner_by_phone() deciding
--                  whether an incoming number belongs to an owner.
--
-- This table is the single source of truth for "does the owner still need
-- to be told about this?" — the request that closes a booking or triggers a
-- handoff never sends WhatsApp itself; it only enqueues a pending row here,
-- so a slow or failing Twilio call can never block the reply to the lead.
-- A separate cron service drains the queue on its own schedule and retries.
--
-- Idempotency is enforced by two partial unique indexes below, one per
-- event_type, instead of application-level locking:
--   - a booking can only ever get ONE notification, ever (booking_id is
--     unique among event_type = 'booking' rows) — a booking is a one-time
--     event, so there is nothing to re-open.
--   - a lead can only have ONE unanswered handoff notification at a time
--     (lead_sender is unique among event_type = 'handoff' rows still
--     missing an owner_response) — once the owner responds, the index no
--     longer applies to that row, so a later handoff from the same lead is
--     free to enqueue a new one.
--
-- owner_phone is denormalized here (copied from owners.owner_phone at
-- enqueue time) so the historical record of "which number did we actually
-- notify" survives the owner later changing their number.
--
-- booking_id is VARCHAR(36) to match trial_bookings.id, which is a uuid4
-- string generated in Python, not a Postgres uuid/SERIAL column. It is
-- NULL for event_type = 'handoff', where there is no booking to reference.
--
-- register_owner_response() only ever writes owner_response here — it
-- deliberately never touches trial_bookings.status. Closing out the
-- booking itself based on the owner's reply is a future feature, not this
-- one.
-- ============================================================

CREATE TABLE IF NOT EXISTS owner_notifications (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    owner_phone VARCHAR(20) NOT NULL,
    event_type VARCHAR(20) NOT NULL,       -- booking | handoff
    lead_sender VARCHAR(20) NOT NULL,
    booking_id VARCHAR(36) NULL REFERENCES trial_bookings(id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    owner_response VARCHAR(20) NULL,        -- confirmed | cancelled
    sent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One notification per booking, ever — the idempotency guard for the
-- booking path (see rationale above).
CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_notifications_booking_unique
ON owner_notifications (booking_id) WHERE event_type = 'booking';

-- At most one unanswered handoff notification per lead at a time — allows a
-- new one only after the previous is answered (see rationale above).
CREATE UNIQUE INDEX IF NOT EXISTS idx_owner_notifications_handoff_open_unique
ON owner_notifications (lead_sender) WHERE event_type = 'handoff' AND owner_response IS NULL;

-- Speeds up the cron's drain query (list_pending_notifications)
CREATE INDEX IF NOT EXISTS idx_owner_notifications_status
ON owner_notifications (status) WHERE status = 'pending';

-- Speeds up the owner-reply lookup (register_owner_response)
CREATE INDEX IF NOT EXISTS idx_owner_notifications_owner_phone
ON owner_notifications (owner_phone);
