-- Test insert for the References feature (insert_reference_messages).
-- Run against Addams sandbox ONLY. Mirrors the exact query shape used by
-- take_five/repository.py: insert_reference_messages().
--
-- Circle: Addams (0bfb1e3e-0dbe-4192-8b53-702f06d94b49)
-- Person: Gomez Addams  (matched sender -> real person_id)
-- External: "Dr. Test External" (unmatched sender -> null person_id, name/org in raw)

-- Row 1: matched sender
INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel, sent_at)
VALUES (
    '0bfb1e3e-0dbe-4192-8b53-702f06d94b49',
    '3d2a5d9b-9a96-42f2-86f8-c7e4a6f9c8b9',  -- Gomez Addams
    'external_reference',
    'inbound',
    'Test message from a matched circle member for the References feature test.',
    '{"thread_label": "references-feature-test", "external_name": null, "external_org": null}'::jsonb,
    'email',
    '2026-08-19 10:55:00-05'
)
RETURNING id, circle_id, person_id, message_type, channel, sent_at;

-- Row 2: external/unmatched sender
INSERT INTO messages (circle_id, person_id, message_type, direction, body, raw, channel, sent_at)
VALUES (
    '0bfb1e3e-0dbe-4192-8b53-702f06d94b49',
    NULL,
    'external_reference',
    'inbound',
    'Test message from an external, unmatched sender for the References feature test.',
    '{"thread_label": "references-feature-test", "external_name": "Dr. Test External", "external_org": "Test Facility"}'::jsonb,
    'email',
    '2026-08-20 10:28:00-05'
)
RETURNING id, circle_id, person_id, message_type, channel, sent_at;

-- After confirming both rows look right, clean up the test data:
-- DELETE FROM messages WHERE circle_id = '0bfb1e3e-0dbe-4192-8b53-702f06d94b49'
--   AND raw->>'thread_label' = 'references-feature-test';
