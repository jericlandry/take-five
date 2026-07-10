-- Migration: rename corroboration_requested -> request_corroboration; drop unused
-- corroboration-response columns (parent_id, response_type). Corroboration is now a
-- single ask-once boolean + timestamp, not a reply-classification chain. Cross-channel
-- dedup (superseded_by_id) is unrelated and stays.
-- Take Five · 2026-07-10

ALTER TABLE clinical_signals
    RENAME COLUMN corroboration_requested TO request_corroboration;

ALTER TABLE clinical_signals
    DROP COLUMN IF EXISTS parent_id,
    DROP COLUMN IF EXISTS response_type;
