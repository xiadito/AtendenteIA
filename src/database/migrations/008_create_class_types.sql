-- ============================================================
-- Migration 008: Create class_types and scheduling_configs, the per-tenant
-- scheduling configuration
--
-- Owner of writes: bot/class_types.py — upsert_class_type(),
--                  delete_class_type(), set_fallback_class_type() and
--                  update_days_ahead(), all reached from the "Aulas" section
--                  of webhook/routes.py's settings screen.
-- Owner of reads:  bot/class_types.py::load_class_types(), called once per
--                  scheduling operation by bot/scheduling.py, and by the four
--                  places that render a class label (webhook/routes.py,
--                  jobs/drain_notifications.py, bot/confirmations.py,
--                  bot/ai_context.py).
--
-- WHY THIS TABLE EXISTS
-- Until Module S2 the class types were two dicts hardcoded at the top of
-- bot/scheduling.py, holding the pilot gym's Jiu-Jitsu classes:
--
--     CLASS_CAPACITY     = {"BABY": 2, "CRIANCAS": 4, "ADULTOS": None}
--     CLASS_TYPE_LABELS  = {"BABY": "Baby Class", ...}
--
-- While that was code, no second gym could exist: a CrossFit box has no way
-- to register a "WOD" class. The seed below reproduces the pilot's three
-- types EXACTLY, so tenant 'default' behaves identically after the move.
--
-- WHAT DID NOT CHANGE
-- Google Calendar is still the only source of truth for WHICH slots exist and
-- WHEN. This table only says which class types a tenant has and how many
-- leads fit in each. The type of a slot still comes from a "[MARKER]" at the
-- start of the event title (bot/scheduling.py::_parse_class_type) — what
-- changed is that the set of valid markers is read from here instead of from
-- a dict.
--
-- marker vs label: _parse_class_type() strips accents and upper-cases the
-- marker it reads from the title before comparing, so `marker` MUST be stored
-- in that same canonical form (no accents, upper case, letters only — the
-- title regex is [a-zA-ZÀ-ÿ]+, so a marker with a digit or a space could
-- never be matched from a title). `label` is the human-readable form, with
-- accents ("Crianças"), and is the only one ever shown to a person.
--
-- capacity NULL = UNLIMITED, and only NULL. get_available_slots() and
-- book_slot() both test `if capacity is not None and active_count >= capacity`,
-- so 0 or -1 as a sentinel would silently mean "always full". The CHECK below
-- keeps a well-meaning UPDATE from inventing one.
--
-- is_fallback is the KeyError guard. _parse_class_type() falls back to a
-- default type for any title without a recognized marker, so that a mis-typed
-- event degrades instead of blocking bookings. When the types lived in code
-- that fallback ("ADULTOS") was guaranteed to be a key of CLASS_CAPACITY;
-- read from a table, it is not — a tenant might have no ADULTOS row at all,
-- and the two direct `CLASS_CAPACITY[class_type]` lookups would raise
-- mid-booking. The flagged row is that tenant's fallback, and the partial
-- unique index below allows at most one per tenant. If a tenant has none,
-- bot/class_types.py::load_class_types() synthesizes an unlimited one in
-- memory rather than letting the lookup fail — see its docstring.
--
-- requires_child_name replaces the hardcoded {"BABY", "CRIANCAS"} set that
-- bot/scheduling.py used to decide whether a booking needs the attending
-- child's name. Left in code, a second gym's "KIDS" class would silently skip
-- that guard.
--
-- scheduling_configs holds days_ahead, the Calendar search horizon, which was
-- a hardcoded default of 14 in get_available_slots(). It is its own table
-- rather than a column on ai_configs (declaredly the UNTRUSTED prompt-text
-- layer) or on owners (credentials — and an ALTER there would break the
-- property that every table in this project is created complete by a single
-- migration). It is where the next scheduling knobs belong.
-- ============================================================

CREATE TABLE IF NOT EXISTS class_types (
    tenant_id           VARCHAR(64) NOT NULL DEFAULT 'default',
    marker              VARCHAR(32) NOT NULL,   -- canonical: no accents, upper case, letters only
    label               VARCHAR(64) NOT NULL,   -- human-readable, with accents
    capacity            INTEGER NULL,           -- NULL = unlimited (see header)
    requires_child_name BOOLEAN NOT NULL DEFAULT FALSE,
    is_fallback         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, marker),
    CONSTRAINT class_types_capacity_positive CHECK (capacity IS NULL OR capacity >= 1)
);

-- At most one fallback type per tenant. The application picks the flagged row
-- as the destination for events whose title has no recognized marker; two
-- flagged rows would make that choice non-deterministic.
CREATE UNIQUE INDEX IF NOT EXISTS idx_class_types_one_fallback_per_tenant
ON class_types (tenant_id) WHERE is_fallback;

-- Seed the pilot tenant with EXACTLY the three types that used to live in
-- bot/scheduling.py, capacities and labels included. ADULTOS is the fallback
-- because it is what _parse_class_type() hardcoded before this migration.
-- ON CONFLICT DO NOTHING so re-running never overwrites the owner's edits.
INSERT INTO class_types (tenant_id, marker, label, capacity, requires_child_name, is_fallback)
VALUES
    ('default', 'BABY',     'Baby Class', 2,    TRUE,  FALSE),
    ('default', 'CRIANCAS', 'Crianças',   4,    TRUE,  FALSE),
    ('default', 'ADULTOS',  'Adultos',    NULL, FALSE, TRUE)
ON CONFLICT (tenant_id, marker) DO NOTHING;

CREATE TABLE IF NOT EXISTS scheduling_configs (
    tenant_id  VARCHAR(64) PRIMARY KEY DEFAULT 'default',
    days_ahead INTEGER NOT NULL DEFAULT 14,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT scheduling_configs_days_ahead_range CHECK (days_ahead BETWEEN 1 AND 90)
);

-- 14 is the value get_available_slots() defaulted to before this migration.
INSERT INTO scheduling_configs (tenant_id, days_ahead)
VALUES ('default', 14)
ON CONFLICT (tenant_id) DO NOTHING;
