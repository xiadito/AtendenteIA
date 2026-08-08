-- ============================================================
-- Migration 009: Create users, and make owners routable by WhatsApp number
--
-- Owner of writes: accounts/provision.py::provision_tenant(), reached from the
--                  founder-run CLI (`python -m accounts.provision create`), and
--                  accounts/bootstrap.py::bootstrap_first_user(), which runs
--                  once at app start from DASHBOARD_USER/DASHBOARD_PASSWORD.
--                  NO WEB ROUTE CREATES A USER — signup is closed on purpose.
-- Owner of reads:  accounts/users.py, reached from accounts/auth.py's
--                  user_loader on every authenticated request and from the
--                  login route. owners.whatsapp_number is read only by
--                  integrations/store.py::find_tenant_by_whatsapp_number(),
--                  called by webhook/routes.py::receive_twilio() on every
--                  inbound message.
--
-- WHY THIS MIGRATION EXISTS
-- Until Module S3a there was no account. "Logged in" was the boolean
-- session["dashboard_authenticated"], set by comparing the submitted password
-- against DASHBOARD_PASSWORD in plain text. One password, one gym, no identity:
-- `messages.author = 'operator'` could not say WHICH human replied. This table
-- is what makes a second gym representable at all.
--
-- 1:1 USER <-> TENANT. tenant_id is a column here, not a join table. One gym,
-- one login. The day a gym needs two operators the answer is a second users row
-- with the same tenant_id — which this shape already allows — not an N:N
-- rewrite.
--
-- email IS THE IDENTIFIER, stored trimmed and lower-cased. The canonical form
-- is defined by accounts/users.py::normalize_email() and applied on BOTH the
-- write path (provisioning) and the read path (login), which is what makes the
-- plain UNIQUE below sufficient — no citext extension, no functional index.
-- Same rule as store.normalize_owner_phone() and class_types.normalize_marker():
-- one place decides what "canonical" means, and everybody goes through it.
--
-- password_hash IS TEXT, NOT VARCHAR(n). werkzeug.security.generate_password_hash
-- defaults to scrypt and produces ~162 characters today, but the format belongs
-- to the library and has already changed once (pbkdf2 -> scrypt in Werkzeug 2.3).
-- A column that is too short does not warn — it raises at the worst moment, in
-- the middle of creating an account.
--
-- THE FK TO owners IS DELIBERATE. owners.tenant_id is NOT NULL UNIQUE (migration
-- 003), so it is a valid FK target. It enforces the invariant that matters: a
-- user always points at a tenant that HAS an owners row, and therefore at a
-- tenant the rest of the system can serve. ON DELETE CASCADE because in a 1:1
-- model a user whose tenant is gone is not a user, it is an orphan that can log
-- in and see nothing. NOTE THE CONSEQUENCE: deleting an owners row deletes the
-- login with it.
--
-- whatsapp_number IS THE ROUTING KEY FOR THE TENANT; owner_phone IS THE ROUTING
-- KEY FOR OWNER-VS-LEAD. Two different numbers with two different jobs, and
-- confusing them corrupts routing in ways nothing reports:
--   * whatsapp_number is the GYM'S OWN Twilio number — the "To" field of an
--     inbound message. It answers "which gym was this written TO?".
--   * owner_phone is the GYM OWNER'S personal number — the "From" field. It
--     answers "is this the owner replying 1/2, or a lead?".
-- Both are stored as plain digits (no "whatsapp:", no "+", no punctuation), the
-- same shape webhook/routes.py compares against and the same shape
-- store.normalize_owner_phone() produces.
--
-- whatsapp_number IS NULLABLE, AND THAT IS THE WHOLE SANDBOX STORY. Today every
-- tenant shares one Twilio Sandbox number, so no tenant can claim it: the column
-- stays NULL, find_tenant_by_whatsapp_number() returns NULL, and
-- webhook/routes.py falls back to tenant 'default' with a WARNING instead of
-- blocking the message. Postgres allows any number of NULLs under a UNIQUE
-- index, so "not configured yet" costs nothing.
--
-- THE owner_phone UNIQUE INDEX IS THE ONE DEFERRED FROM MODULE S1. S1 added the
-- application-side guards (normalize the input, and refuse a number that is
-- already a lead in `sessions`) and left the database constraint to S3, since it
-- runs against a populated table. BEFORE APPLYING THIS MIGRATION, run:
--     SELECT owner_phone, COUNT(*) FROM owners
--     WHERE owner_phone IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1;
-- Anything it returns must be resolved by hand first. Otherwise this migration
-- raises, and because create_app() only PRINTS the init_db() exception the app
-- boots with no users table at all and every login 500s — a failure that looks
-- nothing like its cause.
--
-- UNIQUE INDEXES, NOT ADD CONSTRAINT. `ALTER TABLE ... ADD CONSTRAINT` has no
-- IF NOT EXISTS in Postgres, so re-running this file would abort on the second
-- statement. A unique index enforces exactly the same uniqueness, allows the
-- same multiple NULLs, is inferrable by ON CONFLICT (col), and IS idempotent —
-- which this project treats as a hard requirement rather than a style, because
-- `version` is the filename stem and renaming a migration silently re-runs it.
-- Migration 008 already established the idiom.
--
-- WHAT THIS MIGRATION BREAKS, DELIBERATELY. Every table before this one was
-- created complete by a single migration, with no ALTER follow-ups. 009 alters
-- `owners` twice, because whatsapp_number did not exist as a concept until the
-- one-number-per-gym decision and 003 is already applied everywhere. The
-- "no ALTER follow-ups" note in CLAUDE.md was amended rather than quietly
-- contradicted.
--
-- NO SEED ROW. A password hash cannot be committed to a public repository, so
-- the first user is created at runtime by accounts/bootstrap.py.
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,  -- canonical: trimmed, lower case
    password_hash TEXT NOT NULL,                 -- werkzeug/scrypt; NEVER a plain password
    tenant_id     VARCHAR(64) NOT NULL REFERENCES owners (tenant_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Every request loads a user by id (the user_loader), so the PK covers the hot
-- path. This index is for the founder's CLI, which lists users per tenant, and
-- for Module S3b, which will filter reads by tenant everywhere.
CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users (tenant_id);

-- The gym's own Twilio number: the "To" field of an inbound message, and the
-- key that identifies which tenant the message belongs to.
ALTER TABLE owners ADD COLUMN IF NOT EXISTS whatsapp_number VARCHAR(20);

-- Two tenants sharing an inbound number would make tenant resolution pick one at
-- random. NULL is exempt, which is the sandbox case.
CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_whatsapp_number
ON owners (whatsapp_number);

-- The constraint Module S1 deferred. Two tenants sharing the owner's number
-- would make the owner-vs-lead decision non-deterministic.
CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_owner_phone
ON owners (owner_phone);
