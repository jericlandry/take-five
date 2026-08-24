"""
backfill_channel_identities.py

One-time script to backfill person_channel_identities from the existing
flat people.external_id column (migration 010_channel_identities.sql,
Trello #63). Does NOT modify or drop people.external_id — that column
stays in place, read-only, until every call site is migrated over and the
new OAuth flow (card #39) has run in production without issues.

For each person with a non-null external_id (format "channel:identifier",
e.g. "groupme:123456"):
  1. Parses the channel prefix and identifier.
  2. Inserts a row into person_channel_identities, ON CONFLICT (channel,
     external_id) DO NOTHING — safe to re-run.
  3. Skips (and logs) any external_id that doesn't match the expected
     "channel:identifier" format rather than guessing — malformed data
     should be reviewed manually, not silently backfilled wrong.

Usage:
    python db/backfill/channel_identities.py                # dry run by default
    python db/backfill/channel_identities.py --apply         # actually write
    python db/backfill/channel_identities.py --apply --ensemble-id <uuid>  # scope to one ensemble first

Requirements:
    - .env file in project root with DB_USER / DB_PASSWORD set
    - pip install psycopg2-binary python-dotenv
"""

import argparse
import logging
import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "dbname":   "takefive",
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host":     "dpg-d78po2h5pdvs73b7l7rg-a.virginia-postgres.render.com",
    "port":     5432,
}

VALID_CHANNELS = {"groupme", "whatsapp", "sms", "email"}


def get_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_people_with_external_id(conn, ensemble_id: str = None) -> list:
    query = "SELECT id::text, name, ensemble_id::text, external_id FROM people WHERE external_id IS NOT NULL"
    params = {}
    if ensemble_id:
        query += " AND ensemble_id = %(ensemble_id)s"
        params["ensemble_id"] = ensemble_id
    query += " ORDER BY name;"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def parse_external_id(external_id: str):
    """Returns (channel, identifier) or (None, None) if malformed."""
    if ":" not in external_id:
        return None, None
    channel, identifier = external_id.split(":", 1)
    if channel not in VALID_CHANNELS or not identifier:
        return None, None
    return channel, identifier


def already_backfilled(conn, channel: str, identifier: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM person_channel_identities
            WHERE channel = %(channel)s AND external_id = %(identifier)s;
        """, {"channel": channel, "identifier": identifier})
        return cur.fetchone() is not None


def backfill(apply: bool, ensemble_id: str = None):
    conn = get_connection()
    try:
        people = fetch_people_with_external_id(conn, ensemble_id)
        logger.info(f"Found {len(people)} people with a non-null external_id"
                    f"{f' in ensemble {ensemble_id}' if ensemble_id else ''}")

        backfilled = 0
        already_set = 0
        malformed = []

        for person in people:
            channel, identifier = parse_external_id(person["external_id"])
            if not channel:
                malformed.append((person["name"], person["external_id"]))
                logger.warning(f"  [{person['name']}] Malformed external_id "
                                f"'{person['external_id']}' — skipping, needs manual review")
                continue

            if already_backfilled(conn, channel, identifier):
                logger.info(f"  [{person['name']}] Already backfilled ({channel}:{identifier}) — skipping")
                already_set += 1
                continue

            if not apply:
                logger.info(f"  [DRY RUN] Would insert person_channel_identities row: "
                            f"person={person['name']}, channel={channel}, external_id={identifier}")
                backfilled += 1
                continue

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO person_channel_identities (person_id, channel, external_id)
                    VALUES (%(person_id)s, %(channel)s, %(identifier)s)
                    ON CONFLICT (channel, external_id) DO NOTHING;
                """, {"person_id": person["id"], "channel": channel, "identifier": identifier})
            conn.commit()
            logger.info(f"  [{person['name']}] Backfilled ({channel}:{identifier})")
            backfilled += 1

        logger.info(f"\n{'='*60}")
        logger.info("Backfill complete")
        logger.info(f"  Backfilled:  {backfilled}{' (dry run)' if not apply else ''}")
        logger.info(f"  Already set: {already_set}")
        logger.info(f"  Malformed:   {len(malformed)}{f' — {malformed}' if malformed else ''}")
        logger.info(f"{'='*60}")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill person_channel_identities from people.external_id"
    )
    parser.add_argument("--apply", action="store_true",
                         help="Actually write rows. Omit for a dry run (default).")
    parser.add_argument("--ensemble-id", type=str, default=None,
                         help="Scope to one ensemble (e.g. run against Addams first)")
    args = parser.parse_args()

    if not args.apply:
        logger.info("DRY RUN — no rows will be written. Pass --apply to actually backfill.\n")

    backfill(apply=args.apply, ensemble_id=args.ensemble_id)
