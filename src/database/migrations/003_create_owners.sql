-- ============================================================
-- Migration 003: Create owners table for Google Calendar OAuth
--
-- Owner of writes: integrations/store.py, driven by the OAuth callback
--                  and disconnect routes in integrations/routes.py.
-- Owner of reads:  integrations/routes.py (status page) and, from
--                  Module 2 onward, the scheduling engine.
--
-- Single-tenant pilot: exactly one row exists (tenant_id = 'default'),
-- seeded below so integrations/store.py never has to handle a
-- "no row yet" case. tenant_id stays a real column so a future
-- multi-tenant migration is additive rather than a rewrite.
-- ============================================================

CREATE TABLE IF NOT EXISTS owners (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL UNIQUE DEFAULT 'default',
    google_email VARCHAR(255),
    refresh_token TEXT,
    calendar_id VARCHAR(255),
    integration_status VARCHAR(20) NOT NULL DEFAULT 'disconnected',  -- connected | disconnected | needs_reconnect
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Speeds up the single-row lookup by tenant (already unique, but explicit
-- since every store.py query filters on it)
CREATE INDEX IF NOT EXISTS idx_owners_tenant_id ON owners (tenant_id);

-- Seed the single pilot row so store.py can always assume it exists
INSERT INTO owners (tenant_id, integration_status)
VALUES ('default', 'disconnected')
ON CONFLICT (tenant_id) DO NOTHING;
