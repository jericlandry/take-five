-- 009_chat_membership.sql
-- Take Five · 2026-08-01
--
-- Adds explicit chat-platform-membership tracking to circle_memberships,
-- decoupled from circle membership itself (see Trello #59 discussion,
-- 2026-08-01):
--
--   - circle_memberships = access to the care record (digest, Q&A, roster).
--   - chat_membership_id/chat_added_at = whether this person also has a seat
--     in the circle's chat platform (GroupMe today, WhatsApp planned).
--
-- A person can be a circle_membership without being in the chat (e.g. the
-- senior, or a caregiver who'd rather just text a number) — this is why
-- "add to chat" is a separate, explicit, per-person action rather than
-- automatic on circle_membership creation.
--
-- Named generically (chat_*, not groupme_*) because GroupMe is one of
-- possibly several chat platforms a circle could use (WhatsApp planned) —
-- see take_five/integrations/chat.py, the new dispatch layer this supports.
-- The specific platform is already recorded on the circle itself via
-- care_circles.integration_config, so this table doesn't need to restate it.
--
-- chat_membership_id: the ID returned by the platform's API when the person
--   was added (GroupMe's membership `id` from members/results — NOT the
--   platform user_id). Nullable — NULL means never added to chat.
-- chat_added_at: when the add succeeded. Doubles as the "was this button
--   pressed" flag alongside chat_membership_id.
--
-- DDL is blocked via the Render MCP (read-only) — run this manually via
-- psql, pgAdmin, or the Render console.

ALTER TABLE circle_memberships
    ADD COLUMN chat_membership_id text,
    ADD COLUMN chat_added_at timestamp with time zone;
