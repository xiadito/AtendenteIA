-- ============================================================
-- Migration 010: Create signup_attempts, the public signup throttle
--
-- Owner of writes: accounts/signup.py::record_attempt(), called by
--                  webhook/routes.py::signup() on every POST that gets past the
--                  honeypot — successful or not.
-- Owner of reads:  accounts/signup.py::too_many_attempts(), called by the same
--                  route BEFORE provisioning anything.
--
-- WHY THIS TABLE EXISTS
-- Module S3c opens POST /dashboard/signup to the internet. It is the first
-- unauthenticated endpoint in the project that CREATES ROWS — five tables at a
-- time, through provision_tenant(). Left unthrottled, a loop against it fills
-- `owners`, `ai_configs`, `class_types`, `scheduling_configs` and `users` as
-- fast as the database accepts writes.
--
-- WHY IT IS A TABLE AND NOT A COUNTER IN MEMORY
-- gunicorn runs several workers, each its own Python process with its own
-- module globals. An in-process dict would count roughly 1/N of the attempts
-- per worker and let N times the intended rate through. The database is the
-- only thing the workers share. (Flask-Limiter was rejected for the same
-- reason: doing it properly there needs Redis, which this project does not
-- have, and its in-memory backend has exactly this bug.)
--
-- ip_hash, NOT THE IP. The throttle only ever asks "have I seen this client
-- before?", which equality answers — it never needs to read an address back.
-- An IP is personal data, and this schema is public. accounts/signup.py hashes
-- with FLASK_SECRET_KEY as the salt, so the digests are useless to anyone
-- reading a dump without the app's secret. CHAR(64) is the width of a hex
-- SHA-256.
--
-- NO UNIQUE ANYWHERE, on purpose. One row per attempt is the point: the
-- throttle counts rows in a time window. Collapsing to one row per IP would
-- lose the window.
--
-- THE INDEX IS ON (ip_hash, created_at), IN THAT ORDER. Every query filters by
-- both, equality first: `WHERE ip_hash = %s AND created_at > NOW() - INTERVAL`.
-- Leading with created_at would make it a range scan over every client's
-- attempts.
--
-- THIS TABLE IS NOT PRUNED. It grows one row per signup attempt, forever. At
-- the expected volume that is nothing, and a cleanup job would be more moving
-- parts than the problem deserves — but it is recorded in CLAUDE.md's Known
-- Issues, together with the one-line DELETE that prunes it when it matters.
--
-- MIGRATION RULES (unchanged since S3a): CREATE TABLE/INDEX IF NOT EXISTS only,
-- never ALTER TABLE ... ADD CONSTRAINT — Postgres has no IF NOT EXISTS for it,
-- and `version` is the filename stem, so renaming this file silently re-runs it.
-- ============================================================

CREATE TABLE IF NOT EXISTS signup_attempts (
    id         SERIAL PRIMARY KEY,
    ip_hash    CHAR(64) NOT NULL,   -- hex SHA-256 of (secret + client IP)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Equality on ip_hash first, then the time window. See the header.
CREATE INDEX IF NOT EXISTS idx_signup_attempts_ip_created
ON signup_attempts (ip_hash, created_at);
