-- ============================================================
-- Migration 002: Create products table for synced POS catalog
--
-- Owner of writes: sync_agent (runs on the client's PC; pulls from
--                  the POS Firebird database).
-- Owner of reads:  Flask bot (feeds the AI system prompt with real
--                  product and stock data).
--
-- Soft-delete model: rows are never physically deleted by the sync
--                    agent. When a product disappears from the POS
--                    snapshot, `is_active` is set to FALSE.
-- ============================================================
 
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    external_id VARCHAR(64) NOT NULL UNIQUE,  -- Unique identifier from the POS system
    code VARCHAR(64),
    name VARCHAR(255) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    stock_quantity NUMERIC(12, 3) NOT NULL DEFAULT 0,
    category VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,  -- Soft-delete flag
    last_synced_at TIMESTAMPZ NOT NULL DEFAULT NOW(),  -- Timestamp of the last sync update
);

CREATE INDEX idx_products_external_id ON products (external_id);
CREATE INDEX idx_products_is_active ON products (is_active);
CREATE INDEX idx_products_category ON products (category); 
