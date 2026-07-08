-- Migration: add leads table for homepage pilot signup form
-- Take Five · 2026-07-07

CREATE TABLE IF NOT EXISTS leads (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_type   TEXT NOT NULL CHECK (lead_type IN ('family', 'agency')),
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT,
    details     JSONB NOT NULL DEFAULT '{}',
    source      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
