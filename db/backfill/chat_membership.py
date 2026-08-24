"""
backfill_chat_membership.py

One-time script to backfill circle_memberships.chat_membership_id /
chat_added_at for people who were already added to a circle's GroupMe group
before migration 009_chat_membership.sql existed to track it (e.g. anyone
invited via the old setup_groupme_circle() admin auto-invite, or anyone who
joined GroupMe-natively before the admin-only member lock was added).

Without this, the admin app would show these people as "not yet added to
chat" even though they already are — see Trello #59, 2026-08-01.

For each GroupMe-enabled circle, fetches the group's current member list
directly from GroupMe (GET /groups/:group_id), then for each member:
  1. Tries to match an existing `people` row by external_id
     ('groupme:{user_id}').
  2. Falls back to matching by name within the circle's ensemble (same
     fallback logic as handle_groupme_webhook) for people who were added to
     GroupMe before external_id backfilling existed — links external_id in
     the same step if matched this way.
  3. If matched and the person already has a circle_memberships row for
     this circle, backfills chat_membership_id + chat_added_at (using now()
     as the backfill timestamp, since the true original add time isn't
     knowable from GroupMe's API — logged clearly as a backfill, not a real
     add time).
  4. Unmatched members (no people row, and no name match) are logged for
     manual review — this script never creates new people rows or
     circle_memberships rows, only backfills the two new columns onto
     existing memberships. Creating people/memberships is exactly the
     silent-duplicate risk Trello #59 exists to prevent; a backfill script
     is not the place to make that judgment call.

Usage:
    python db/backfill/chat_membership.py                    # list GroupMe-enabled circles
    python db/backfill/chat_membership.py --circle-id <uuid>  # backfill one circle
    python db/backfill/chat_membership.py --circle-id <uuid> --dry-run

Requirements:
    - .env file in project root with DB_USER / DB_PASSWORD and
      GROUPME_USER_ACCESS_TOKEN set
    - pip install httpx psycopg2-binary python-dotenv
"""

import argparse
import logging
import os

import httpx
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

GROUPME_API_BASE = "https://api.groupme.com/v3"


def get_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def list_groupme_circles(conn) -> list:
    """List all circles with a GroupMe group configured."""
    query = """
        SELECT c.id::text AS id, c.name, c.ensemble_id::text AS ensemble_id,
               e.name AS ensemble_name,
               c.integration_config->>'groupme_group_id' AS group_id
        FROM care_circles c
        JOIN ensembles e ON e.id = c.ensemble_id
        WHERE c.integration_config->>'groupme_group_id' IS NOT NULL
        ORDER BY e.name, c.name;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall()


def get_circle(conn, circle_id: str) -> dict:
    query = """
        SELECT c.id::text AS id, c.name, c.ensemble_id::text AS ensemble_id,
               c.integration_config->>'groupme_group_id' AS group_id
        FROM care_circles c
        WHERE c.id = %(circle_id)s;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"circle_id": circle_id})
        return cur.fetchone()


def fetch_groupme_members(group_id: str) -> list:
    """
    GET /groups/:group_id — returns the group's current member list. Each
    member has 'id' (per-group membership id — what circle_memberships.
    chat_membership_id stores), 'user_id' (global GroupMe account id — what
    people.external_id stores, as 'groupme:{user_id}'), and 'nickname'.
    """
    token = os.getenv("GROUPME_USER_ACCESS_TOKEN")
    if not token:
        raise ValueError("GROUPME_USER_ACCESS_TOKEN not set in environment")

    with httpx.Client() as client:
        resp = client.get(
            f"{GROUPME_API_BASE}/groups/{group_id}",
            params={"token": token},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"GroupMe group fetch failed: {resp.status_code} {resp.text}")
    return resp.json().get("response", {}).get("members", [])


def find_person_by_external_id(conn, external_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, name, external_id FROM people WHERE external_id = %(eid)s;",
                    {"eid": external_id})
        return cur.fetchone()


def find_person_by_name(conn, ensemble_id: str, name: str) -> dict:
    """Same fallback used by handle_groupme_webhook — case-insensitive name
    match within the ensemble, only among people with no external_id yet."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id::text, name, external_id FROM people
            WHERE ensemble_id = %(ensemble_id)s
              AND external_id IS NULL
              AND LOWER(name) = LOWER(%(name)s)
            LIMIT 1;
        """, {"ensemble_id": ensemble_id, "name": name})
        return cur.fetchone()


def get_circle_membership(conn, circle_id: str, person_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id::text, chat_membership_id, chat_added_at
            FROM circle_memberships
            WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s;
        """, {"circle_id": circle_id, "person_id": person_id})
        return cur.fetchone()


def backfill_circle(circle_id: str, dry_run: bool):
    conn = get_connection()
    try:
        circle = get_circle(conn, circle_id)
        if not circle:
            logger.error(f"No circle found with id {circle_id}")
            return
        if not circle["group_id"]:
            logger.error(f"Circle '{circle['name']}' has no GroupMe group configured")
            return

        logger.info(f"Backfilling chat membership for '{circle['name']}' (group {circle['group_id']})")
        members = fetch_groupme_members(circle["group_id"])
        logger.info(f"Found {len(members)} member(s) in the GroupMe group")

        matched = 0
        already_set = 0
        name_matched = 0
        unresolved = []

        for member in members:
            user_id = member.get("user_id")
            membership_id = member.get("id")
            nickname = member.get("nickname", "Unknown")

            if not user_id or not membership_id:
                logger.warning(f"  Skipping malformed member entry: {member}")
                continue

            external_id = f"groupme:{user_id}"
            person = find_person_by_external_id(conn, external_id)
            matched_by_name = False

            if not person:
                # Fallback: name match within the ensemble, same as the
                # webhook's own fallback for people who were added before
                # external_id backfilling existed.
                person = find_person_by_name(conn, circle["ensemble_id"], nickname)
                matched_by_name = bool(person)

            if not person:
                unresolved.append(nickname)
                logger.warning(f"  [{nickname}] No matching person found (external_id or name) — needs manual review")
                continue

            person_id = person["id"]

            if matched_by_name:
                logger.info(f"  [{nickname}] Matched by name to existing person '{person['name']}' — will link external_id")
                name_matched += 1

            membership = get_circle_membership(conn, circle_id, person_id)
            if not membership:
                logger.warning(f"  [{nickname}] Matched to person '{person['name']}' but they have no "
                                f"circle_membership for this circle — skipping (in GroupMe group but not a circle member)")
                continue

            if membership["chat_membership_id"] and membership["chat_added_at"]:
                logger.info(f"  [{nickname}] Already backfilled — skipping")
                already_set += 1
                continue

            if dry_run:
                logger.info(f"  [DRY RUN] Would backfill '{person['name']}': "
                            f"chat_membership_id={membership_id}, external_id={'(link)' if matched_by_name else '(already set)'}")
                matched += 1
                continue

            with conn.cursor() as cur:
                if matched_by_name:
                    cur.execute(
                        "UPDATE people SET external_id = %(eid)s WHERE id = %(id)s;",
                        {"eid": external_id, "id": person_id},
                    )
                cur.execute("""
                    UPDATE circle_memberships SET
                        chat_membership_id = %(mid)s,
                        chat_added_at = now()
                    WHERE circle_id = %(circle_id)s AND person_id = %(person_id)s;
                """, {"mid": membership_id, "circle_id": circle_id, "person_id": person_id})
            conn.commit()
            logger.info(f"  [{nickname}] Backfilled '{person['name']}' (chat_membership_id={membership_id})")
            matched += 1

        logger.info(f"\n{'='*60}")
        logger.info(f"Backfill complete for '{circle['name']}'")
        logger.info(f"  Backfilled:        {matched}{' (dry run)' if dry_run else ''}")
        logger.info(f"  Matched by name:   {name_matched}")
        logger.info(f"  Already set:       {already_set}")
        logger.info(f"  Unresolved:        {len(unresolved)}{f' — {unresolved}' if unresolved else ''}")
        logger.info(f"{'='*60}")

    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill circle_memberships chat_membership_id/chat_added_at from live GroupMe group state")
    parser.add_argument("--circle-id", type=str, default=None, help="Circle UUID to backfill. Omit to list GroupMe-enabled circles.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be backfilled without writing to DB")
    args = parser.parse_args()

    if not args.circle_id:
        conn = get_connection()
        circles = list_groupme_circles(conn)
        conn.close()
        print("\nGroupMe-enabled circles:")
        for c in circles:
            print(f"  {c['id']}  {c['ensemble_name']} / {c['name']}  (group {c['group_id']})")
        print("\nRun with: python db/backfill/chat_membership.py --circle-id <uuid>")
    else:
        backfill_circle(args.circle_id, dry_run=args.dry_run)
