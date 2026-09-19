"""
colby_checkin_history.py

One-time script to backfill a week of realistic GroupMe check-in history
into the Colby Family circle (demo ensemble, for the product demo video).

Unlike a raw SQL INSERT, this writes messages with explicit historical
sent_at timestamps AND leaves them exactly as a real inbound message would
look -- direction='inbound', message_type='inbound', channel='groupme' --
so downstream real-pipeline steps behave normally against them:

  - generate_weekly_digest() / ask_with_tools()'s Recent Messages window
    both read messages straight from the DB (no embeddings needed), so
    these are usable immediately after this script runs.
  - Clinical signal detection does NOT run automatically here (it's only
    triggered by run_post_storage_pipeline() on the live webhook path) --
    run db/backfill/signals.py against the Colby ensemble afterward to
    detect signals against these messages for real, with detected_at
    correctly backdated to each message's sent_at.

Usage:
    python db/backfill/colby_checkin_history.py               # dry run by default
    python db/backfill/colby_checkin_history.py --write        # actually insert

Requirements:
    - .env file in project root with DB_USER / DB_PASSWORD set
    - pip install psycopg2-binary python-dotenv
"""

import argparse
import logging
import os
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname":   "takefive",
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     "dpg-d78po2h5pdvs73b7l7rg-a.virginia-postgres.render.com",
    "port":     5432,
}

COLBY_FAMILY_CIRCLE_ID = "877bf913-da49-4de0-9eb8-75b9543d52d4"

JANET_HAYES_ID = "dbe5363c-cecd-418d-8ef6-5e78af7c9d61"  # caregiver
KATE_COLBY_ID  = "682d5f9c-83e3-4cfc-91a5-b35395b5571c"  # family

# (person_id, body, sent_at) -- sent_at in America/Chicago local time,
# written as ISO strings with explicit -05 offset for readability.
MESSAGES = [
    (JANET_HAYES_ID,
     "Frank ate a full breakfast and lunch today, good appetite. Blood pressure meds "
     "taken on schedule. He mentioned wanting to finish his Grisham novel this week.",
     "2026-09-09T09:40:00-05:00"),

    (KATE_COLBY_ID,
     "Thanks Janet! I'll bring him the next one in the series when I visit Saturday.",
     "2026-09-09T18:12:00-05:00"),

    (JANET_HAYES_ID,
     "Barbara had a good day, watched her stories after lunch and took a short walk "
     "in the backyard. Mood was upbeat.",
     "2026-09-11T13:05:00-05:00"),

    (JANET_HAYES_ID,
     "Noticed Frank's ankle looked a little swollen again after his walk today. Gave "
     "him his usual dose on schedule. Nothing urgent but wanted to flag it.",
     "2026-09-12T15:50:00-05:00"),

    (KATE_COLBY_ID,
     "Talked to Dr. Yiou's office, Dad's appointment is confirmed for next Thursday.",
     "2026-09-13T11:20:00-05:00"),

    (JANET_HAYES_ID,
     "Barbara finished the puzzle she'd been working on all week! She was really "
     "proud of herself.",
     "2026-09-13T16:30:00-05:00"),

    (JANET_HAYES_ID,
     "Quiet day today, both resting most of the afternoon. Aide visit went smoothly, "
     "no concerns.",
     "2026-09-14T14:15:00-05:00"),

    (JANET_HAYES_ID,
     "Frank's swelling looks better today. Back to his usual energy, walked out to "
     "the mailbox on his own this morning.",
     "2026-09-15T10:05:00-05:00"),
]


def get_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def insert_messages(conn, circle_id: str, messages: list, dry_run: bool) -> int:
    inserted = 0
    with conn.cursor() as cur:
        for person_id, body, sent_at_str in messages:
            sent_at = datetime.fromisoformat(sent_at_str)
            preview = body[:70].replace("\n", " ")
            logger.info(f"  {sent_at.strftime('%b %d %I:%M %p')} — {preview}...")

            if dry_run:
                continue

            cur.execute("""
                INSERT INTO messages (circle_id, person_id, message_type, direction, body, channel, sent_at)
                VALUES (%(circle_id)s, %(person_id)s, 'inbound', 'inbound', %(body)s, 'groupme', %(sent_at)s)
                RETURNING id;
            """, {
                "circle_id": circle_id,
                "person_id": person_id,
                "body":      body,
                "sent_at":   sent_at,
            })
            row = cur.fetchone()
            logger.info(f"    -> inserted message {row['id']}")
            inserted += 1

    if not dry_run:
        conn.commit()

    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Colby Family check-in history")
    parser.add_argument("--write", action="store_true",
                         help="Actually insert. Without this flag, only previews what would be inserted.")
    args = parser.parse_args()

    dry_run = not args.write

    conn = get_connection()
    try:
        logger.info(f"{'DRY RUN — ' if dry_run else ''}Inserting {len(MESSAGES)} messages into Colby Family ({COLBY_FAMILY_CIRCLE_ID})")
        count = insert_messages(conn, COLBY_FAMILY_CIRCLE_ID, MESSAGES, dry_run)
        if dry_run:
            logger.info(f"\nDRY RUN complete — nothing written. Re-run with --write to insert for real.")
        else:
            logger.info(f"\nInserted {count} messages.")
            logger.info(
                "Next: run clinical signal detection against these for real —\n"
                "  python db/backfill/signals.py --ensemble-id 6de9ac2f-54e9-4f0a-9a6f-3750baf2b2ce"
            )
    finally:
        conn.close()
