-- 013_senior_digest_message_type.sql
-- Take Five · 2026-09-16
--
-- Adds 'senior_digest' to messages.message_type's allowed set.
--
-- send_senior_emails() (main_summary.py), via repo.log_message(), logs
-- each senior-facing weekly email with msg_type='senior_digest' after a
-- successful send_email() call. The insert path was written assuming
-- this value was already permitted -- it isn't; discovered via a
-- CheckViolation on the first real send to John Landry (Landry circle,
-- 2026-09-16) -- same failure mode as migrations 011 and 012. The email
-- itself sent successfully (SendGrid call happens before this log write);
-- only the audit-trail row was lost.
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
        'member_added'::text,
        'senior_digest'::text
    ]));
