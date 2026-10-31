-- 015_care_recipient_rename.sql
-- Take Five · 2026-10-31
--
-- Step 2 of 2 (see 014_care_recipient_widen.sql).
--
-- RUN RIGHT AFTER the code change is live. Until this runs, the new code
-- finds no care recipients (it looks for role = 'care_recipient'), so signal
-- detection, prep packets, and care-recipient emails skip those circles --
-- keep the gap to minutes, and not across the Sunday 22:00 UTC digest cron.
--
-- One transaction: renames the stored values, then drops 'senior_digest'
-- from the allowed message types. Expected counts on 2026-10-31:
-- UPDATE 14 (roles), UPDATE 53 (digest messages).
--
-- Not renamed on purpose: the 'senior_person_id' key inside prep-packet
-- messages.raw JSONB (stored data read by key name, not a role value).

BEGIN;

UPDATE public.circle_memberships SET role = 'care_recipient' WHERE role = 'senior';

UPDATE public.messages SET message_type = 'care_recipient_digest'
 WHERE message_type = 'senior_digest';

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
        'care_recipient_digest'::text
    ]));

COMMIT;
