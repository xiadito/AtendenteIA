--- Migration 001: create base tables for sessions and orders
--- Runs exactly once - tracked in the schema_migrations table

CREATE TABLE IF NOT EXISTS sessions (
    sender VARCHAR(20) PRIMARY KEY,
    history JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(36) PRIMARY KEY,
    sender VARCHAR(20) NOT NULL,
    items JSONB NOT NULL,
    total NUMERIC(10, 2) NOT NULL,
    client_address TEXT NOT NULL,
    current_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Speeds up order lookups filtered by costumer
CREATE INDEX IF NOT EXISTS idx_orders_sender
ON orders(sender);

-- Speeds up dashboard sorting by date 
CREATE INDEX IF NOT EXISTS idx_orders_created_at
ON orders(created_at);