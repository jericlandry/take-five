-- Test setup: backfill appointment_date on the two most recent Addams Family
-- prep packets (Morticia + Gomez, Dr Yu, "appointment tomorrow" from 7/13)
-- so the post-visit follow-up tier has something to find during manual testing.

-- Morticia Addams
UPDATE messages
SET raw = raw || '{"appointment_date": "2026-07-14"}'::jsonb
WHERE id = '5362d7eb-a097-495e-aec3-80f63adcfbee';

-- Gomez Addams
UPDATE messages
SET raw = raw || '{"appointment_date": "2026-07-14"}'::jsonb
WHERE id = 'e4e0bebd-ba04-4989-883a-b9730d2d616b';

-- Verify
SELECT id, raw->>'doctor_name' AS doctor_name, raw->>'senior_person_id' AS senior_person_id,
       raw->>'appointment_date' AS appointment_date
FROM messages
WHERE id IN ('5362d7eb-a097-495e-aec3-80f63adcfbee', 'e4e0bebd-ba04-4989-883a-b9730d2d616b');
