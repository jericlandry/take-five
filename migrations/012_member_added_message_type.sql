-- 012_member_added_message_type.sql
-- Take Five · 2026-08-26
--
-- Adds 'member_added' to messages.message_type's allowed set.
--
-- New-member digest visibility fix (repository.log_membership_event,
-- called from add_person_to_circle, invite_person_to_ensemble, and
-- add_person_to_groupme in take_five/integrations/groupme.py, plus the
-- webhook's silent first-post auto-add): logs a family-facing event when
-- someone joins a circle (event_type='joined_circle') or its chat platform
-- (event_type='joined_chat'), so the weekly digest's new "NEW IN THE
-- CIRCLE" section has something to read. The insert path was written
-- assuming this value was already permitted -- it isn't; discovered via a
-- CheckViolation on first real end-to-end test against the Addams sandbox
-- (same failure mode as migration 011).
--
-- DDL is blocked via the Render MCP (read-only) -- run this manually via
-- psql, pgAdmin, or the Render console.

ALTER TABLE public.messages DROP CONSTRAINT messages_message_type_check;

ALTER TABLE public.messages ADD CONSTRAINT messages_message_type_check
    CHECK (message_type = ANY (ARRAY[
        'inbound'::text,
        'check_in'::text,
        'digest'::text,
        'agent_note'::text,
        'prep_packet'::text,
        'external_reference'::text,
        'member_added'::text
    ]));
