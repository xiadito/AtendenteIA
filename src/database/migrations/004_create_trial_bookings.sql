-- ============================================================
-- Migration 004: Create trial_bookings table for the scheduling engine
--
-- Owner of writes: bot/bookings.py, called from bot/scheduling.py::book_slot()
--                  under a Postgres advisory lock (see create_booking_with_lock).
-- Owner of reads:  bot/bookings.py (active-booking counts feed
--                  bot/scheduling.py::get_available_slots(), which decides
--                  whether a Calendar event still has open seats).
--
-- Google Calendar remains the source of truth for which slots exist, when,
-- and of what type (via the event title marker). This table is only the
-- reservation ledger: how many leads are booked into each Calendar event,
-- and who they are. There is no availability_slots table by design.
--
-- id is a VARCHAR(36) holding a uuid4 generated in Python (see
-- bookings.create_booking_with_lock) rather than a SERIAL, so a booking's id is
-- known before the INSERT and never leaks how many bookings exist.
--
-- calendar_event_id has no UNIQUE constraint on its own: a single event can
-- accept more than one booking once class capacity > 1 (baby/kids classes).
-- UNIQUE(calendar_event_id, sender) instead stops the same lead from
-- reserving the same slot twice, without limiting how many different leads
-- can book it.
--
-- child_name is set only for [BABY]/[CRIANCAS] classes, which are attended by a
-- minor: lead_name/sender is the responsible adult who chats on WhatsApp, and
-- child_name is the child who takes the class. It is NULLABLE on purpose: NULL
-- means "not applicable" (an adult booking), distinct from an empty string
-- standing for "a child class whose name wasn't collected".
-- ============================================================

CREATE TABLE IF NOT EXISTS trial_bookings (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
    sender VARCHAR(20) NOT NULL,
    lead_name VARCHAR(255) NOT NULL,
    child_name VARCHAR(255) NULL,
    calendar_event_id VARCHAR(255) NOT NULL,
    class_type VARCHAR(20) NOT NULL,
    slot_start TIMESTAMPTZ NOT NULL,
    slot_end TIMESTAMPTZ NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending_confirmation',  -- pending_confirmation | confirmed | cancelled
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (calendar_event_id, sender)
);

-- Speeds up lead-facing lookups ("does this lead already have a booking?")
CREATE INDEX IF NOT EXISTS idx_trial_bookings_sender
ON trial_bookings (sender);

-- Speeds up upcoming-bookings queries and dashboard sorting
CREATE INDEX IF NOT EXISTS idx_trial_bookings_slot_start
ON trial_bookings (slot_start);

-- Speeds up the per-event active-count query that backs both
-- get_available_slots() and the advisory-lock capacity check in book_slot()
CREATE INDEX IF NOT EXISTS idx_trial_bookings_calendar_event_id
ON trial_bookings (calendar_event_id);
