-- Take Five · POC schema
-- Run against your Supabase / Postgres instance

create extension if not exists "pgcrypto";

-- ── people ─────────────────────────────────────────────────────────────────
-- Unifies seniors, family members, aides, nurses, and the agent itself.
-- type: 'senior' | 'family' | 'aide' | 'nurse' | 'agent'
create table people (
    id          uuid primary key default gen_random_uuid(),
    external_id text unique, -- e.g. GroupMe user ID for correlation
    external_type text,       -- e.g. 'groupme'
    name        text not null,
    email       text,
    phone       text,
    type        text not null check (type in ('senior', 'family', 'aide', 'nurse', 'agent')),
    timezone    text not null default 'America/Chicago',
    created_at  timestamptz not null default now()
);

-- ── care_circles ───────────────────────────────────────────────────────────
-- One circle per senior. senior_id references the person at the center.
create table care_circles (
    id          uuid primary key default gen_random_uuid(),
    senior_id   uuid not null references people (id),
    name        text not null,
    -- status: 'active' | 'paused' | 'archived'
    status      text not null default 'active' check (status in ('active', 'paused', 'archived')),
    created_at  timestamptz not null default now()
);

-- ── circle_memberships ─────────────────────────────────────────────────────
-- Who belongs to a circle and in what role.
-- role: 'primary' | 'family' | 'aide' | 'nurse' | 'agent'
create table circle_memberships (
    id          uuid primary key default gen_random_uuid(),
    circle_id   uuid not null references care_circles (id) on delete cascade,
    person_id   uuid not null references people (id),
    role        text not null,
    sms_active  boolean not null default true,
    joined_at   timestamptz not null default now(),
    unique (circle_id, person_id)
);

-- ── messages ───────────────────────────────────────────────────────────────
-- The single conversation store for a care circle.
--
-- message_type: 'inbound'    raw message from GroupMe, not yet classified
--               'check_in'   caregiver update, parsed by the agent
--               'digest'     weekly summary authored by the agent
--               'agent_note' ad-hoc agent observation
--
-- direction:    'inbound' | 'outbound'
-- channel:      'groupme' | 'sms' | 'email' (v2 expansion)
--
-- parsed:       populated by the agent for check_in messages.
--               shape: {
--                 mood_score: 1-5,
--                 meds_taken: bool,
--                 ate_well:   bool,
--                 notes:      string,
--                 life_log:   [{ category, content }]
--               }
--
-- person_id:    null when authored by the system/agent with no membership row
create table messages (
    id              uuid primary key default gen_random_uuid(),
    circle_id       uuid not null references care_circles (id) on delete cascade,
    person_id       uuid references people (id),
    message_type    text not null check (message_type in ('inbound', 'check_in', 'digest', 'agent_note')),
    direction       text not null check (direction in ('inbound', 'outbound')),
    channel         text not null default 'groupme',
    body            text not null,
    parsed          jsonb,
    sent_at         timestamptz not null default now()
);

-- ── indexes ────────────────────────────────────────────────────────────────
-- Everything the weekly digest query will need.
create index on messages (circle_id, sent_at desc);
create index on messages (circle_id, message_type);
create index on circle_memberships (circle_id);
create index on circle_memberships (person_id);
create index on care_circles (senior_id);
