-- 016_sms_consents.sql
-- Take Five · 2026-11-14
--
-- Consent records for texting programs, starting with native group-text
-- circles (see the project doc claude/sms-group-onboarding-design.md and
-- Trello card "Store SMS consent records from opt-in pages").
--
-- Why a new table (the project prefers not to add state tables): this is a
-- legal audit record. Carriers and the TCPA expect proof of each opt-in --
-- who, which program, exactly what they agreed to, how it was verified, and
-- when -- and that proof has to survive later edits to people/circles. It's
-- append-only: a new row per decision (consent, decline), with revoked_at
-- set on STOP. The latest row per (person, circle, program) is that
-- person's current state.
--
-- program: 'sms_group' (group-text circles). Leaves room for the existing
--   toll-free relay program ('sms_relay') to record consent the same way.
-- consented: true = checked the box; false = declined (unchecked and
--   continued). Both are recorded; a decline is a real answer.
-- consent_version / consent_text_hash: which wording they saw, so a later
--   copy change doesn't rewrite history.
-- verified_via: 'otp' (code sent to that phone and entered on the page).
--
-- DDL is blocked via the Render MCP (read-only) -- run this manually via
-- psql or the Render console.

CREATE TABLE public.sms_consents (
    id                uuid DEFAULT gen_random_uuid() NOT NULL,
    person_id         uuid NOT NULL,
    circle_id         uuid NOT NULL,
    phone             text NOT NULL,
    program           text NOT NULL,
    consented         boolean NOT NULL,
    consent_version   text NOT NULL,
    consent_text_hash text NOT NULL,
    page_url          text,
    verified_via      text NOT NULL,
    ip                text,
    user_agent        text,
    created_at        timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at        timestamp with time zone,
    CONSTRAINT sms_consents_pkey PRIMARY KEY (id),
    CONSTRAINT sms_consents_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES public.people(id) ON DELETE CASCADE,
    CONSTRAINT sms_consents_circle_id_fkey
        FOREIGN KEY (circle_id) REFERENCES public.care_circles(id) ON DELETE CASCADE,
    CONSTRAINT sms_consents_program_check
        CHECK (program = ANY (ARRAY['sms_group'::text, 'sms_relay'::text])),
    CONSTRAINT sms_consents_verified_via_check
        CHECK (verified_via = ANY (ARRAY['otp'::text]))
);

-- "Current state for this person in this circle's program" lookups.
CREATE INDEX sms_consents_lookup_idx
    ON public.sms_consents USING btree (circle_id, program, person_id, created_at DESC);

-- STOP handling looks up by phone.
CREATE INDEX sms_consents_phone_idx
    ON public.sms_consents USING btree (phone);
