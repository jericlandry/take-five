-- 010_channel_identities.sql
-- Take Five · 2026-08-03
--
-- Replaces people.external_id (a single flat column, one identity per
-- person, implicitly GroupMe-only) with two tables that generalize across
-- channels — see Trello #63, 2026-08-02 design discussion.
--
-- person_channel_identities: a person's identity on a given channel
--   ("groupme:123456" becomes channel='groupme', external_id='123456').
--   One row per (person, channel-identity) combination — a person can have
--   more than one identity on the SAME channel (e.g. two GroupMe accounts
--   over time from a changed phone number), and can have identities on
--   multiple channels at once (GroupMe + WhatsApp + email). Unique on
--   (channel, external_id), not on (person_id, channel) — this is what
--   allows both of those cases without a schema change.
--
--   High-frequency table: read on every inbound webhook message
--   (handle_groupme_webhook's person lookup). Needs a real indexed lookup,
--   which is the reason this is its own table and not a JSONB column on
--   people — a JSONB blob can't cleanly enforce cross-person uniqueness the
--   way a real UNIQUE constraint does, and would need per-key expression
--   indexes to stay fast.
--
-- person_channel_credentials: a stored access token/credential for a
--   person on a channel — currently only GroupMe needs this (the per-admin
--   OAuth token from card #39's design). person_id is DELIBERATELY
--   NULLABLE: most channels (WhatsApp via Meta or Twilio, default-case
--   email via a transactional service) authenticate with a single
--   platform-level credential, not a per-person one — those cases don't
--   use this table at all and keep using plain app config (env vars),
--   same as GROUPME_USER_ACCESS_TOKEN does today. Only GroupMe populates
--   person_id-scoped rows here for now.
--
-- people.external_id is NOT dropped in this migration. It stays in place,
-- unused by new code once call sites are migrated, until card #39's OAuth
-- flow has run in production without issues — dropped in a later, separate
-- migration once that's confirmed, not as part of this data move.
--
-- DDL is blocked via the Render MCP (read-only) — run this manually via
-- psql, pgAdmin, or the Render console.

CREATE TABLE public.person_channel_identities (
    id          uuid DEFAULT gen_random_uuid() NOT NULL,
    person_id   uuid NOT NULL,
    channel     text NOT NULL,
    external_id text NOT NULL,
    created_at  timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT person_channel_identities_pkey PRIMARY KEY (id),
    CONSTRAINT person_channel_identities_channel_external_id_key UNIQUE (channel, external_id),
    CONSTRAINT person_channel_identities_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES public.people(id) ON DELETE CASCADE,
    CONSTRAINT person_channel_identities_channel_check
        CHECK (channel = ANY (ARRAY['groupme'::text, 'whatsapp'::text, 'sms'::text, 'email'::text]))
);

CREATE INDEX person_channel_identities_person_id_idx
    ON public.person_channel_identities USING btree (person_id);

-- Lookup by (channel, external_id) is covered by the UNIQUE constraint's
-- implicit index — no separate index needed for that direction.

CREATE TABLE public.person_channel_credentials (
    id           uuid DEFAULT gen_random_uuid() NOT NULL,
    person_id    uuid,
    channel      text NOT NULL,
    access_token text NOT NULL,
    obtained_at  timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT person_channel_credentials_pkey PRIMARY KEY (id),
    CONSTRAINT person_channel_credentials_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES public.people(id) ON DELETE CASCADE,
    CONSTRAINT person_channel_credentials_channel_check
        CHECK (channel = ANY (ARRAY['groupme'::text, 'whatsapp'::text, 'sms'::text, 'email'::text])),
    -- A person can only have one stored credential per channel — a fresh
    -- OAuth login for the same person/channel should replace the old row,
    -- not create a second one (see repo.upsert_person_channel_credential).
    CONSTRAINT person_channel_credentials_person_id_channel_key UNIQUE (person_id, channel)
);

CREATE INDEX person_channel_credentials_person_id_idx
    ON public.person_channel_credentials USING btree (person_id)
    WHERE person_id IS NOT NULL;
