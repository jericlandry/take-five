-- Migration: add date_of_birth to people
-- Take Five · 2026-05-26

ALTER TABLE people
    ADD COLUMN IF NOT EXISTS date_of_birth DATE;
