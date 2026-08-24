-- 011_external_reference_message_type.sql
-- Take Five · 2026-08-24
--
-- Adds 'external_reference' to messages.message_type's allowed set.
--
-- Take Five References feature (superadmin /circles/{id}/reference-threads
-- and ensemble-admin /app/circles/{id}/reference-threads): backfills email
-- threads and documents that predate ingestion into the messages table,
-- tagged message_type='external_reference' so the digest generator can
-- exclude this content from "what happened this week" summaries while
-- ask() and decision support can still retrieve it. The insert path
-- (repository.insert_reference_messages) was written assuming this value
-- was already permitted — it isn't; discovered via a CheckViolation on
-- first real end-to-end test against the Addams sandbox.
--
-- DDL is blocked via the Render MCP (read-only) — run this manually via
-- psql, pgAdmin, or the Render console.

ALTER TABLE public.messages DROP CONSTRAINT messages_message_type_check;

ALTER TABLE public.messages ADD CONSTRAINT messages_message_type_check
    CHECK (message_type = ANY (ARRAY[
        'inbound'::text,
        'check_in'::text,
        'digest'::text,
        'agent_note'::text,
        'prep_packet'::text,
        'external_reference'::text
    ]));
