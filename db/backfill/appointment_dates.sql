-- One-time backfill: set appointment_date on the two most recent Landry
-- prep packets (Mom + Dad, Dr. Yu, today's appointment) so the post-visit
-- follow-up tier can pick them up. Everything else in `raw` is preserved.

-- Mary Ellen Landry (Mom) — prep packet sent 2026-07-13 02:17:46 UTC
UPDATE messages
SET raw = raw || '{"appointment_date": "2026-07-13"}'::jsonb
WHERE id = '5de0a41f-829b-4bbb-847f-822c73765a44';

-- John Landry (Dad) — prep packet sent 2026-07-13 00:51:43 UTC
UPDATE messages
SET raw = raw || '{"appointment_date": "2026-07-13"}'::jsonb
WHERE id = '9089302f-f8db-41d9-be56-c953637ccb71';

-- Verify
SELECT id, raw->>'doctor_name' AS doctor_name, raw->>'senior_person_id' AS senior_person_id,
       raw->>'appointment_date' AS appointment_date
FROM messages
WHERE id IN ('5de0a41f-829b-4bbb-847f-822c73765a44', '9089302f-f8db-41d9-be56-c953637ccb71');
