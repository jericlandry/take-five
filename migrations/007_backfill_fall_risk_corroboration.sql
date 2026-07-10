-- Backfill: flip existing fall-risk signals to request_corroboration = true.
-- These were classified under the old corroboration rule, which blanket-excluded
-- everything in signal_category = 'safety' — including risk/concern assessments,
-- not just discrete incidents. Fall risk is a judgment call, not a fact, and
-- these five (four re: John Landry, one re: Mary Ellen Landry) have been sitting
-- unsurfaced. The updated detection prompt (take_five/signals.py,
-- backfill_signals.py) prevents this going forward; this corrects what's already
-- in the table.
-- Take Five · 2026-07-10

UPDATE clinical_signals
SET request_corroboration = true
WHERE id IN (
    '61125bc7-a9dd-45fd-be39-2727e79b6c48',  -- "feels unsteady and thinks she will fall" (John Landry)
    '646bb4ff-7227-4279-a7db-da925c445d11',  -- "she scares me going up n down those 2 steps" (John Landry)
    'a1c31335-5082-4cf5-97b8-ebb217def3f9',  -- "registering a bit of instability too" (John Landry)
    'a01985d5-3e91-41cf-9da4-f5fa9c1776c0',  -- "had to correct him on shuffling, especially in the community" (John Landry)
    '69b2cd83-5f72-4d7f-ae88-dfec0d959a05'   -- "concerned about fall risk though she only took it intermittently" (Mary Ellen Landry)
)
AND request_corroboration = false;
