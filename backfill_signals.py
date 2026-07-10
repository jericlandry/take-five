"""
backfill_signals.py

One-time script to extract clinical signals from all historical messages.
Run locally against the production DB.

Usage:
    python backfill_signals.py                    # Addams Family only (safe default)
    python backfill_signals.py --ensemble landry  # Landry Family only
    python backfill_signals.py --ensemble all     # Both ensembles
    python backfill_signals.py --dry-run          # Print what would be processed, no DB writes

Requirements:
    - .env file in project root with DATABASE_URL set
    - pip install anthropic psycopg2-binary python-dotenv
"""

import argparse
import asyncio
import json
import logging
import re
import time
from typing import Optional

import psycopg2
import psycopg2.extras
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
import os

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

DETECTION_PROMPT = """You are a clinical signal detector for Take Five, an AI care coordination platform for families supporting aging loved ones.

Your job is to read a single message from a family care circle and extract any health or safety observations about the people being cared for. You are not diagnosing — you are identifying observations worth tracking.

SUBJECTS IN THIS CARE CIRCLE:
{subjects}

SIGNAL TAXONOMY:
- safety: falls, injuries, emergencies, near-misses, fall risk
- functional: mobility, eating, sleep, cognition, dressing, energy, withdrawal from activities, hearing
- symptom: pain, nausea, dizziness, shortness of breath, physical complaints
- mood: affect, tearfulness, agitation, flat affect, good days, anxiety
- medication: refusals, missed doses, side effects, changes, new prescriptions

MENTION STYLE:
- direct: the sender is reporting their own firsthand observation — "I heard him coughing", "I saw her fall", "she wouldn't eat when I was there"
- oblique: the sender is relaying what someone else said or reported — "he said his neck hurts", "she told me she didn't sleep", "Jennifer mentioned Mom had fallen", "I heard from the aide that she fell"

CONFIDENCE:
Score 0.0-1.0. Lower for hedged language, third-party reports, ambiguous observations. Higher for direct firsthand statements of discrete events.

RULES:
- Extract ALL signals — a single message may contain many
- Only extract signals about the identified subjects, not family members or caregivers
- Capture signals even when phrased as questions, secondhand reports, or hedged observations — "what did the nurse say about dad's nipples?" is a symptom signal; "she seems a little off" is a mood signal
- Capture refusals of mobility aids (wheelchair, walker, cane) as functional/mobility signals
- Capture incontinence mentions, accidents, or protective bedding needs as functional signals
- Do not diagnose — report what was observed or mentioned
- Return [] ONLY for messages with zero health or safety content: pure logistics, technology troubleshooting, genealogy research, social banter, scheduling, book or TV discussion with no health context
- Do NOT return [] just because a signal is soft, oblique, secondhand, or mentioned in passing — those are exactly the signals worth capturing
- Do NOT return [] for messages that contain physical complaints, symptom mentions, mobility observations, medication notes, or behavioral changes — even minor ones
- If the same event or observation is mentioned more than once in a message, extract it only once — choose the most informative excerpt
- Keep raw_excerpt short — 10 to 15 words maximum, just enough to identify the signal. Do not quote full sentences.

WHAT NOT TO FLAG:
- Mood: Do not flag general positive affect or enjoyment — "good spirits", "enjoyed her meal", "having a great time", "all smiles" are NOT clinical signals. Only flag significant mood changes, emotional distress, agitation, tearfulness, or anxiety.
- Medication: Do not flag medication logistics — purchasing medications, filling pill trays, scheduling doses, confirming what was bought, or OTC purchases like pain relievers, supplements, or items bought at a pharmacy or grocery store. Only flag new prescriptions, discontinuations, refusals, missed doses, side effects, or compliance issues.
- Functional: Do not flag routine daily activities — reading, watching TV, eating meals, attending social events, going to church — as functional signals unless they represent a notable change from baseline or the person is visibly struggling. "She enjoyed her meatloaf" is not a signal. "She ran out of steam halfway through the outing" is a signal. Do not flag family coordination decisions about care — "we should get Lucy to help with dressing" or "we are adding more aide time" are logistics, not observations about the subject. Only flag direct observations of the subject struggling, declining, or changing.

CORROBORATION:
Set corroboration_suggested to true when:
- mention_style is oblique AND confidence is below 0.80
- the signal is secondhand or reported speech ("she said", "he mentioned", "I heard")
- the observation is ambiguous enough that confirmation from another circle member would meaningfully change how it should be interpreted

Set corroboration_suggested to false when:
- signal_category is safety OR signal_type contains fall, injury, fracture, emergency, 911, firemen, ambulance — never corroborate hard incidents regardless of mention style or confidence
- confidence is 0.85 or above with direct mention style
- the signal comes from a professional caregiver's firsthand observation
- the signal is a discrete, specific event (a fall happened, a medication was refused) even if reported secondhand — the event either happened or it didn't, corroboration won't change that

Return ONLY a valid JSON array. No preamble, no explanation, no markdown, no code fences. Do not wrap output in backticks of any kind. Do not reconsider or add commentary after the array. Raw JSON array only, nothing else. If no signals found, return []."""


def get_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def get_messages_to_process(conn, ensemble_name: str) -> list:
    """
    Fetch all substantive inbound messages for an ensemble that don't
    already have clinical_signal records.
    """
    query = """
        SELECT 
            m.id::text as message_id,
            m.circle_id::text as circle_id,
            m.body,
            m.channel,
            m.sent_at,
            e.name as ensemble_name
        FROM messages m
        JOIN care_circles cc ON m.circle_id = cc.id
        JOIN ensembles e ON cc.ensemble_id = e.id
        WHERE e.name = %(ensemble_name)s
        AND m.direction = 'inbound'
        AND length(trim(m.body)) > 20
        AND m.body NOT LIKE '@T5%%'
        AND m.body NOT LIKE '@t5%%'
        AND NOT EXISTS (
            SELECT 1 FROM clinical_signals cs
            WHERE cs.message_id = m.id
        )
        ORDER BY m.sent_at ASC;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"ensemble_name": ensemble_name})
        return cur.fetchall()


def get_seniors(conn, circle_id: str) -> list:
    """Fetch seniors for a circle."""
    query = """
        SELECT p.id::text as id, p.name, p.aliases
        FROM people p
        JOIN circle_memberships cm ON p.id = cm.person_id
        WHERE cm.circle_id = %(circle_id)s
        AND cm.role = 'senior'
        ORDER BY p.name;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"circle_id": circle_id})
        return cur.fetchall()


def build_subjects_string(seniors: list) -> str:
    if not seniors:
        return "Unknown"
    parts = []
    for s in seniors:
        name = s["name"]
        aliases = s.get("aliases") or []
        if aliases:
            parts.append(f"{name} ({', '.join(aliases)})")
        else:
            parts.append(name)
    return ", ".join(parts)


def resolve_subject_id(subject_name: str, seniors: list) -> Optional[str]:
    subject_lower = subject_name.lower()
    for senior in seniors:
        if subject_lower in senior["name"].lower():
            return senior["id"]
        aliases = senior.get("aliases") or []
        for alias in aliases:
            if subject_lower in alias.lower():
                return senior["id"]
    return None


def strip_and_parse(raw: str) -> list:
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'\s*```$', '', raw).strip()
    first_end = raw.find("]")
    if first_end != -1:
        raw = raw[:first_end + 1].strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        last_close = raw.rfind("}")
        if last_close > 0:
            try:
                recovered = json.loads(raw[:last_close + 1] + "]")
                return recovered if isinstance(recovered, list) else []
            except json.JSONDecodeError:
                pass
        return []


def save_signal(conn, message_id: str, circle_id: str, subject_id: Optional[str], signal: dict, channel: str):
    query = """
        INSERT INTO clinical_signals (
            message_id, circle_id, subject_id,
            signal_category, signal_type,
            raw_excerpt, mention_style, confidence,
            channel, request_corroboration
        ) VALUES (
            %(message_id)s, %(circle_id)s, %(subject_id)s,
            %(signal_category)s, %(signal_type)s,
            %(raw_excerpt)s, %(mention_style)s, %(confidence)s,
            %(channel)s, %(request_corroboration)s
        )
        RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "message_id":             message_id,
            "circle_id":              circle_id,
            "subject_id":             subject_id,
            "signal_category":        signal["signal_category"],
            "signal_type":            signal["signal_type"],
            "raw_excerpt":            signal.get("raw_excerpt"),
            "mention_style":          signal.get("mention_style"),
            "confidence":             signal.get("confidence"),
            "channel":                channel or "groupme",
            "request_corroboration":  signal.get("corroboration_suggested", False),
        })
        return cur.fetchone()


async def process_message(client: AsyncAnthropic, conn, msg: dict, seniors: list, dry_run: bool) -> int:
    """Process a single message. Returns number of signals written."""
    subjects_str = build_subjects_string(seniors)
    prompt = DETECTION_PROMPT.replace("{subjects}", subjects_str)

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": prompt + "\n\n---\n\nMESSAGE TO ANALYZE:\n" + msg["body"]
            }]
        )
        raw = response.content[0].text.strip()
        signals = strip_and_parse(raw)

        if not signals:
            return 0

        if dry_run:
            logger.info(f"  [DRY RUN] Would write {len(signals)} signal(s):")
            for s in signals:
                logger.info(f"    {s.get('subject_name')} | {s.get('signal_category')} | {s.get('signal_type')} | {s.get('raw_excerpt', '')[:60]}")
            return len(signals)

        count = 0
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            if not signal.get("signal_category") or not signal.get("signal_type"):
                continue
            subject_id = resolve_subject_id(signal.get("subject_name", ""), seniors)
            save_signal(conn, msg["message_id"], msg["circle_id"], subject_id, signal, msg.get("channel"))
            count += 1

        conn.commit()
        return count

    except Exception as e:
        conn.rollback()
        logger.error(f"  Error processing message {msg['message_id']}: {e}")
        return 0


async def run_backfill(ensemble_names: list, dry_run: bool, delay: float = 0.75):
    conn = get_connection()
    client = AsyncAnthropic()

    total_messages = 0
    total_signals = 0
    total_skipped = 0

    try:
        for ensemble_name in ensemble_names:
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing ensemble: {ensemble_name}")
            logger.info(f"{'='*60}")

            messages = get_messages_to_process(conn, ensemble_name)
            logger.info(f"Found {len(messages)} messages to process (already-processed messages skipped)")

            if not messages:
                logger.info("Nothing to process.")
                continue

            # Cache seniors per circle to avoid repeated DB calls
            seniors_cache = {}

            for i, msg in enumerate(messages, 1):
                circle_id = msg["circle_id"]

                if circle_id not in seniors_cache:
                    seniors_cache[circle_id] = get_seniors(conn, circle_id)

                seniors = seniors_cache[circle_id]
                if not seniors:
                    logger.info(f"  [{i}/{len(messages)}] Skipping — no seniors in circle {circle_id}")
                    total_skipped += 1
                    continue

                preview = msg["body"][:80].replace("\n", " ")
                logger.info(f"  [{i}/{len(messages)}] {msg['sent_at'].strftime('%b %d')} — {preview}...")

                count = await process_message(client, conn, msg, seniors, dry_run)
                total_messages += 1
                total_signals += count

                if count > 0:
                    logger.info(f"    → {count} signal(s) {'(dry run)' if dry_run else 'written'}")
                else:
                    logger.info(f"    → no signals detected")

                # Rate limit buffer
                await asyncio.sleep(delay)

    finally:
        conn.close()

    logger.info(f"\n{'='*60}")
    logger.info(f"Backfill complete")
    logger.info(f"  Messages processed: {total_messages}")
    logger.info(f"  Signals written:    {total_signals}")
    logger.info(f"  Skipped (no seniors): {total_skipped}")
    if dry_run:
        logger.info(f"  DRY RUN — nothing was written to the database")
    logger.info(f"{'='*60}")


def list_ensembles(conn) -> list:
    """Print all available ensembles with their IDs."""
    with conn.cursor() as cur:
        cur.execute("SELECT id::text, name FROM ensembles ORDER BY name;")
        return cur.fetchall()


def get_ensemble_name(conn, ensemble_id: str) -> Optional[str]:
    """Resolve ensemble name from ID."""
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM ensembles WHERE id = %(id)s;", {"id": ensemble_id})
        row = cur.fetchone()
        return row["name"] if row else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill clinical signals from historical messages")
    parser.add_argument(
        "--ensemble-id",
        type=str,
        default=None,
        help="Ensemble UUID to process. Omit to list available ensembles."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be processed without writing to DB"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.75,
        help="Seconds to wait between API calls (default: 0.75)"
    )
    args = parser.parse_args()

    conn = get_connection()

    if not args.ensemble_id:
        ensembles = list_ensembles(conn)
        conn.close()
        print("\nAvailable ensembles:")
        for e in ensembles:
            print(f"  {e['id']}  {e['name']}")
        print("\nRun with: python backfill_signals.py --ensemble-id <uuid>")
    else:
        ensemble_name = get_ensemble_name(conn, args.ensemble_id)
        conn.close()
        if not ensemble_name:
            print(f"No ensemble found with ID: {args.ensemble_id}")
            print("Run without --ensemble-id to list available ensembles.")
        else:
            asyncio.run(run_backfill([ensemble_name], dry_run=args.dry_run, delay=args.delay))
