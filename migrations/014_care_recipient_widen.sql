-- 014_care_recipient_widen.sql
-- Take Five · 2026-10-31
--
-- Step 1 of 2 for renaming the 'senior' concept to 'care_recipient'
-- (circle_memberships.role 'senior' -> 'care_recipient';
--  messages.message_type 'senior_digest' -> 'care_recipient_digest').
--
-- RUN BEFORE deploying the code change. Widens the message_type CHECK so it
-- accepts BOTH the old and new digest values; changes no data. Safe with the
-- old code still running. circle_memberships.role has no CHECK constraint
-- (validated in main.py), so nothing to widen there.
--
-- Then deploy, then run 015_care_recipient_rename.sql right away.
--
-- DDL is blocked via the Render MCP (read-only) -- run manually via psql,
-- pgAdmin, or the Render console.

ALTER TABLE public.messages DROP CONSTRAINT messages_message_type_check;

ALTER TABLE public.messages ADD CONSTRAINT messages_message_type_check
    CHECK (message_type = ANY (ARRAY[
        'inbound'::text,
        'check_in'::text,
        'digest'::text,
        'agent_note'::text,
        'prep_packet'::text,
        'external_reference'::text,
        'member_added'::text,
        'senior_digest'::text,
        'care_recipient_digest'::text
    ]));
