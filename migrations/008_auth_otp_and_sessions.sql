-- Migration: OTP codes + long-lived sessions for SMS-OTP auth on /app/...
-- routes, replacing the email-lookup auth pattern. Also normalizes existing
-- people.phone values to E.164 so OTP lookups match on the same format.
-- Take Five · 2026-07-22

CREATE TABLE IF NOT EXISTS otp_codes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone       text NOT NULL,
    code_hash   text NOT NULL,
    attempts    integer NOT NULL DEFAULT 0,
    expires_at  timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at  timestamp with time zone NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS otp_codes_phone_created_idx
    ON otp_codes (phone, created_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id     uuid NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    token_hash    text NOT NULL UNIQUE,
    created_at    timestamp with time zone NOT NULL DEFAULT now(),
    last_used_at  timestamp with time zone NOT NULL DEFAULT now(),
    expires_at    timestamp with time zone NOT NULL,
    revoked_at    timestamp with time zone
);

CREATE INDEX IF NOT EXISTS sessions_token_hash_idx ON sessions (token_hash);
CREATE INDEX IF NOT EXISTS sessions_person_id_idx  ON sessions (person_id);

-- Normalize existing people.phone to E.164 (+1XXXXXXXXXX) so OTP lookups,
-- which normalize the caller's input the same way, actually match.
-- Sampled prod data (2026-07-22): most rows already +1XXXXXXXXXX, a few
-- bare 10-digit, a couple short test/placeholder values (e.g. '5551212'),
-- some null/empty. Both statements below are no-ops on anything that isn't
-- a clean 10 or 11-digit (leading 1) US number, so malformed/test rows are
-- left untouched rather than guessed at.

UPDATE people
SET phone = '+1' || regexp_replace(phone, '\D', '', 'g')
WHERE phone IS NOT NULL
  AND length(regexp_replace(phone, '\D', '', 'g')) = 10;

UPDATE people
SET phone = '+' || regexp_replace(phone, '\D', '', 'g')
WHERE phone IS NOT NULL
  AND length(regexp_replace(phone, '\D', '', 'g')) = 11
  AND regexp_replace(phone, '\D', '', 'g') LIKE '1%';
