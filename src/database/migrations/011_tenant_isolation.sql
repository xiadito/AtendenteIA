-- ============================================================
-- Migration 011: tenant-scoped keys — the schema half of Module S3b
--
-- Owner of writes/reads: bot/session.py, bot/messages.py, bot/bookings.py.
--
-- WHY THIS EXISTS
-- Module S3a made a second gym CREATABLE: accounts, login, provision_tenant(),
-- and a tenant resolved from Twilio's "To" field. It did NOT make a second gym
-- SAFE. `sessions` was keyed by `sender` alone, so the same lead writing to two
-- gyms could only exist once; `messages` referenced that single-column key, so a
-- message belonged to a phone number rather than to a conversation at a gym; and
-- `trial_bookings`' UNIQUE (calendar_event_id, sender) was global. This file
-- moves all three keys to include the tenant, which is what lets the reads in
-- Module S3b filter by it and actually mean something.
--
-- THE ORDER BELOW IS THE WHOLE MIGRATION. It runs against a table that already
-- holds the pilot's data, and the steps depend on each other:
--
--   1. sessions gains tenant_id. The DEFAULT backfills every existing row to
--      'default' in the same statement — no separate UPDATE, no window in which
--      the column is NULL.
--   2. messages.tenant_id is realigned to its session's tenant. Today a no-op
--      (everything is 'default'), but it is what guarantees step 5's foreign key
--      cannot fail on real data: pre-011 the single-column FK already promised
--      every message has a session, so this UPDATE covers all of them.
--   3. The messages -> sessions foreign key is DROPPED. It has to go before the
--      primary key it depends on; leaving it would make step 4 fail.
--   4. sessions' primary key becomes (tenant_id, sender).
--   5. The foreign key comes back COMPOSITE, still ON DELETE CASCADE — which
--      bot/session.py::clear_session() and every suite teardown depend on.
--   6. trial_bookings' UNIQUE becomes tenant-scoped.
--   7. Indexes are rebuilt with tenant_id in FRONT, because every query after
--      S3b filters by it first.
--
-- Nothing is deleted and nothing is rewritten except the realignment in step 2.
-- database/db.py runs each .sql file in a single execute() followed by one
-- commit, so the whole thing is one transaction: it either applies completely or
-- leaves the database untouched.
--
-- WHY THE DO BLOCKS. The house rule is "never ALTER TABLE ... ADD CONSTRAINT,
-- always CREATE UNIQUE INDEX IF NOT EXISTS", because `version` is the filename
-- stem and a rename silently re-runs the file, so every migration must be
-- idempotent. A PRIMARY KEY and a composite FOREIGN KEY cannot be expressed as
-- plain indexes, so the same guarantee is bought a different way: each one is
-- wrapped in a DO block that asks pg_constraint whether the target state is
-- already there. The file therefore CONVERGES — it produces the same schema
-- from a database sitting at 010 and from one already carrying these keys.
--
-- Constraint names are DISCOVERED, never hardcoded: `UNIQUE (a, b)` written
-- inline in migrations 004/007 got a Postgres-generated name, and an earlier
-- hand-run attempt could have left a differently-named equivalent behind.
-- ============================================================

-- 1. sessions gains the tenant. NOT NULL DEFAULT 'default' backfills in place.
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default';


-- 2. Realign every message to its session's tenant, so the composite foreign key
--    in step 5 has nothing to reject. A no-op while 'default' is the only
--    tenant; correctness insurance the day it is not.
UPDATE messages m
SET tenant_id = s.tenant_id
FROM sessions s
WHERE m.sender = s.sender
  AND m.tenant_id IS DISTINCT FROM s.tenant_id;


-- 3. Drop whatever foreign key messages has onto sessions. Discovered, because
--    migration 007 wrote it inline and let Postgres name it.
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid = 'messages'::regclass
          AND confrelid = 'sessions'::regclass
    LOOP
        EXECUTE format('ALTER TABLE messages DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;


-- 4. Swap the primary key of sessions to (tenant_id, sender). The guard makes a
--    re-run a no-op instead of an error.
DO $$
DECLARE
    existing_pk TEXT;
    pk_columns TEXT;
BEGIN
    SELECT conname,
           pg_get_constraintdef(oid)
      INTO existing_pk, pk_columns
      FROM pg_constraint
     WHERE contype = 'p'
       AND conrelid = 'sessions'::regclass;

    IF existing_pk IS NULL THEN
        ALTER TABLE sessions ADD PRIMARY KEY (tenant_id, sender);
    ELSIF pk_columns <> 'PRIMARY KEY (tenant_id, sender)' THEN
        EXECUTE format('ALTER TABLE sessions DROP CONSTRAINT %I', existing_pk);
        ALTER TABLE sessions ADD PRIMARY KEY (tenant_id, sender);
    END IF;
END $$;


-- 5. Bring the foreign key back composite, with the CASCADE that clear_session()
--    and every suite teardown rely on. Named explicitly this time.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'messages_tenant_sender_fkey'
           AND conrelid = 'messages'::regclass
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT messages_tenant_sender_fkey
            FOREIGN KEY (tenant_id, sender)
            REFERENCES sessions (tenant_id, sender)
            ON DELETE CASCADE;
    END IF;
END $$;


-- 6. trial_bookings: the "one lead books one slot once" rule becomes per tenant.
--    Two gyms are two calendars, so the same (event, lead) pair in different
--    tenants is a different fact and must be allowed.
--
--    Dropped by discovery — migration 004 wrote UNIQUE (calendar_event_id,
--    sender) inline — and recreated as a unique INDEX, the idiom migrations 008
--    and 009 already use. It enforces the same uniqueness, is inferrable by
--    ON CONFLICT, and still raises the UniqueViolation that
--    bookings.create_booking_with_lock() catches to report "duplicate".
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE contype = 'u'
          AND conrelid = 'trial_bookings'::regclass
          AND pg_get_constraintdef(oid) IN (
              'UNIQUE (calendar_event_id, sender)',
              'UNIQUE (tenant_id, calendar_event_id, sender)'
          )
    LOOP
        EXECUTE format('ALTER TABLE trial_bookings DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_bookings_tenant_event_sender
ON trial_bookings (tenant_id, calendar_event_id, sender);


-- 7. Indexes, rebuilt with tenant_id in front. Every read after S3b filters by
--    the tenant first, so an index that leads with `sender` no longer serves the
--    query it was built for.

-- Serves the operator opening a full thread AND the handler taking the last N
-- rows for the LLM payload — both now scoped to one tenant.
CREATE INDEX IF NOT EXISTS idx_messages_tenant_sender_created_at
ON messages (tenant_id, sender, created_at);

DROP INDEX IF EXISTS idx_messages_sender_created_at;

-- The inbox's unread badge. Same tiny partial index, one column wider.
DROP INDEX IF EXISTS idx_messages_unread_by_lead;

CREATE INDEX IF NOT EXISTS idx_messages_unread_by_lead
ON messages (tenant_id, sender) WHERE author = 'lead' AND is_read = FALSE;

-- Funnel-style lookups by stage, per gym.
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_stage
ON sessions (tenant_id, stage);

DROP INDEX IF EXISTS idx_sessions_stage;

-- "Does this lead already have a booking?" — asked inside one gym.
CREATE INDEX IF NOT EXISTS idx_trial_bookings_tenant_sender
ON trial_bookings (tenant_id, sender);

DROP INDEX IF EXISTS idx_trial_bookings_sender;
