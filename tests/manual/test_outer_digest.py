"""
One-off test script for the outer-circle redacted digest feature.
Run from the take-five project root with the venv active.

Creates a sandbox outer circle under Addams, seeds test messages (both
clinical and non-clinical) into the inner circle plus one into the outer
circle itself, generates the outer digest, and directly tests the
deterministic safety scan against known-bad text.
"""
from datetime import datetime, timezone
from take_five.repository import repo
from take_five.summaries import generate_outer_weekly_digest, _contains_restricted_content

ADDAMS_INNER_ID = '0bfb1e3e-0dbe-4192-8b53-702f06d94b49'

# 1. Create (or reuse) a sandbox outer circle under Addams
existing = repo._execute(
    "SELECT id FROM care_circles WHERE parent_circle_id = %(pid)s LIMIT 1;",
    {'pid': ADDAMS_INNER_ID},
)
if existing:
    outer_id = str(existing['id'])
    outer_check = repo.get_circle_by_id(outer_id)
    if not outer_check.get('external_id'):
        repo.update_care_circle(outer_id, {'external_id': f'test:outer-addams-{outer_id[:8]}'})
        print('Backfilled missing external_id on existing outer circle:', outer_id)
    else:
        print('Reusing existing outer circle:', outer_id)
else:
    ensemble_id = repo.get_circle_by_id(ADDAMS_INNER_ID)['ensemble_id']
    outer = repo.create_care_circle(
        ensemble_id=ensemble_id,
        name='Addams Family & Friends',
        parent_circle_id=ADDAMS_INNER_ID,
    )
    outer_id = str(outer['id'])
    # Sandbox circle -- never actually connected to real GroupMe, so give it
    # a fake external_id purely so log_message()'s external_id subquery can
    # resolve it (log_message has no direct circle_id parameter).
    repo.update_care_circle(outer_id, {'external_id': f'test:outer-addams-{outer_id[:8]}'})
    print('Created outer circle:', outer_id)

inner_circle = repo.get_circle_by_id(ADDAMS_INNER_ID)
outer_circle = repo.get_circle_by_id(outer_id)

# 2. Seed test messages -- deliberately mixing clinical and non-clinical
#    content in the INNER circle, plus a visit-only message in the OUTER
#    circle itself, to check both directions get combined correctly.
repo.log_message(
    circle_ext_id=inner_circle['external_id'],
    person_ext_id=None,
    person_id=None,
    body='Gomez had his cardiology follow-up Tuesday. BP was 138/88 and they adjusted his metoprolol dosage.',
    msg_type='inbound',
    direction='inbound',
    channel='groupme',
)
repo.log_message(
    circle_ext_id=inner_circle['external_id'],
    person_ext_id=None,
    person_id=None,
    body='Wednesday stopped by Thursday afternoon and they played chess and watched an old movie together. Gomez seemed in great spirits.',
    msg_type='inbound',
    direction='inbound',
    channel='groupme',
)
repo.log_message(
    circle_ext_id=outer_circle['external_id'],
    person_ext_id=None,
    person_id=None,
    body='Lurch brought dinner over Friday evening and stayed to visit for a couple hours.',
    msg_type='inbound',
    direction='inbound',
    channel='groupme',
)
print('Seeded 3 test messages (2 inner, 1 outer).')

# 3. Generate the outer digest and inspect it
result = generate_outer_weekly_digest(
    outer_id,
    start_date=datetime.now(timezone.utc).replace(hour=0, minute=0),
)
print()
print('=== OUTER DIGEST RESULT ===')
print('blocked:', result['blocked'])
print('flagged_terms:', result['flagged_terms'])
print()
print(result['digest'])

# 4. Directly test the safety net against known-bad text, independent of
#    whatever the LLM actually produced above -- proves the scan itself
#    works even if the prompt somehow got it right on this particular run.
print()
print('=== DIRECT SAFETY SCAN TEST ===')
bad_text = "MEL — Good week. BP was 137/82 and her metoprolol dose was increased."
hits = _contains_restricted_content(bad_text)
print('Test string:', bad_text)
print('Flagged terms:', hits)
print('Would block:', bool(hits))
