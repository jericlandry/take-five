"""
take_five/engagement/post_visit.py — Post-visit appointment follow-up.

Highest-priority tier in the engagement cron's priority order (see
take_five/engagement/checks.py) — checked before clinical signal
corroboration and Life Log. A concrete, time-bound loop (did the appointment
happen, did anyone report back) outranks both.

Every day, scans prep packets from the last week, finds ones whose
appointment already happened (1-7 days ago) and haven't been asked about or
organically covered yet, and surfaces them for a single combined ask —
grounded in the top items from each packet's "RAISE WITH" section, never a
generic "how'd it go."

Multiple prep packets for the same visit (one per senior, or duplicate
attempts while a family member refined their request) are deduped to the
most recent per senior, then combined into one message rather than firing
separately — a family doesn't get pinged twice for one appointment.
"""

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from anthropic import AsyncAnthropic

from take_five.repository import repo
from take_five.utils import get_prompt

logger = logging.getLogger(__name__)

SEARCH_WINDOW_DAYS = 7   # how far back to look for prep packets at all
MIN_DAYS_PAST = 1        # appointment must be at least this many days in the past
MAX_DAYS_PAST = 7        # ...and no more than this many days in the past — too
                         # stale beyond this, same reasoning as the other tiers'
                         # bounded lookback windows.

ALREADY_REPORTED_PROMPT = get_prompt("already_reported_prompt")


def _strip_and_parse(raw: str) -> Optional[dict]:
    """Same fence/commentary stripping pattern used in signals.py and life_log.py."""
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'\s*```$', '', raw).strip()
    last_end = raw.rfind("}")
    if last_end != -1:
        raw = raw[:last_end + 1]
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _format_messages(rows: list) -> str:
    if not rows:
        return "_No messages since the prep packet._"
    lines = []
    for row in rows:
        sent = row.get("sent_at")
        ts = sent.strftime("%b %d") if sent else "unknown"
        author = row.get("author_name") or "Unknown"
        lines.append(f"- **{author}** ({ts}): {row.get('body', '')}")
    return "\n".join(lines)


async def _already_reported(packet: Dict, circle_id: str, reference_time: datetime) -> bool:
    """
    LLM check: has anyone posted anything since this prep packet that looks
    like a report on how the appointment went? Fails open (returns False,
    i.e. "not yet reported") on error — a spurious follow-up ask is a much
    smaller cost than silently dropping a real one.
    """
    since_messages = [
        m for m in repo.get_messages(circle_id, start_date=packet["sent_at"], end_date=reference_time)
        if m.get("direction") == "inbound"
    ]
    if not since_messages:
        return False

    prompt = ALREADY_REPORTED_PROMPT.format(
        packet_body=packet["body"],
        messages=_format_messages(since_messages),
    )
    try:
        client = AsyncAnthropic()
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        parsed = _strip_and_parse(raw)
        return bool(parsed and parsed.get("already_reported"))
    except Exception as e:
        logger.error(f"[post_visit] Already-reported check failed for packet {packet.get('id')}: {e}", exc_info=True)
        return False


def _dedupe_by_senior(packets: List[Dict]) -> List[Dict]:
    """
    Keep only the most recent prep packet per senior — collapses duplicate or
    refined requests for the same visit (a real pattern seen in pilot data:
    a family member re-requesting prep 3-4 times while correcting the doctor
    name or splitting by senior).

    Keyed by raw.senior_person_id when available; falls back to
    doctor_name + appointment_date for packets where senior resolution didn't
    stamp an ID (an earlier, separately-tracked bug — see Coal Mine).
    """
    best: Dict[str, Dict] = {}
    for p in packets:
        raw = p.get("raw") or {}
        key = raw.get("senior_person_id") or f"{raw.get('doctor_name')}|{raw.get('appointment_date')}"
        if key not in best or p["sent_at"] > best[key]["sent_at"]:
            best[key] = p
    return list(best.values())


async def find_due_followups(circle_id: str, as_of: Optional[datetime] = None) -> List[Dict]:
    """
    Returns the prep-packet rows due for a post-visit follow-up right now:
    appointment 1-7 days in the past, not yet flagged (asked or covered),
    deduped to one per senior, and not already organically reported on
    (packets found to already be covered are flagged 'covered' as a side
    effect here so they're never rechecked).

    as_of: evaluate as if this were the current time instead of the real
    current time (see main_engagement.py --as-of). Defaults to real now().
    """
    reference_time = as_of or datetime.now(timezone.utc)
    today = reference_time.date()

    since = reference_time - timedelta(days=SEARCH_WINDOW_DAYS)
    packets = repo.get_prep_packets(circle_id, since=since)

    candidates = []
    for p in packets:
        raw = p.get("raw") or {}
        if raw.get("followup_status"):
            continue  # already asked or already marked covered

        appt_date_str = raw.get("appointment_date")
        if not appt_date_str:
            continue  # no resolvable date on this packet — never guess

        try:
            appt_date = date.fromisoformat(appt_date_str)
        except ValueError:
            logger.warning(f"[post_visit] Packet {p['id']} has unparseable appointment_date: {appt_date_str!r}")
            continue

        days_past = (today - appt_date).days
        if not (MIN_DAYS_PAST <= days_past <= MAX_DAYS_PAST):
            continue

        candidates.append(p)

    candidates = _dedupe_by_senior(candidates)

    due = []
    for p in candidates:
        if await _already_reported(p, circle_id, reference_time):
            repo.mark_prep_packet_followup(p["id"], "covered")
            logger.info(f"[post_visit] Packet {p['id']}: already reported on organically — marking covered, not asking.")
            continue
        due.append(p)

    return due
