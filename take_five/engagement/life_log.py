"""
take_five/engagement/life_log.py — Life Log content extraction.

This is Tier 2 in the engagement cron's priority order (see
take_five/engagement/checks.py) — only evaluated when Tier 1 (pending
clinical signal corroboration) has nothing to say. Two-level extraction,
most specific first:

  1. Recent unresolved thread (last 14 days) — a concrete, specific detail
     someone mentioned that never got a follow-up: a planned activity, an
     upcoming event, a stated plan. Time-bound, so bounded to a recent window —
     asking about something that already happened weeks ago feels stale.
  2. Durable personal detail (full history) — an enduring interest, hobby, or
     relationship detail that doesn't go stale the way a resolved event does,
     so the wider lookback is fine here.

If neither extraction finds anything grounded, this returns None. There is no
generic/category fallback — every fact used in the resulting question must
trace back to an actual message. Silence is the correct outcome when nothing
specific exists to reference (see card: Lull Detection & Proactive Check-in).
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from anthropic import AsyncAnthropic

from take_five.repository import repo
from take_five.utils import get_prompt

logger = logging.getLogger(__name__)

RECENT_WINDOW_DAYS = 14

RECENT_THREAD_PROMPT = get_prompt("recent_thread_prompt")

DURABLE_DETAIL_PROMPT = get_prompt("durable_detail_prompt")


def _format_messages(rows: list, limit: int = 150) -> str:
    if not rows:
        return "_No messages found._"
    lines = []
    for row in rows[:limit]:
        sent = row.get("sent_at")
        ts = sent.strftime("%b %d") if sent else "unknown"
        author = row.get("author_name") or "Unknown"
        lines.append(f"- **{author}** ({ts}): {row.get('body', '')}")
    return "\n".join(lines)


def _strip_and_parse(raw: str) -> Optional[dict]:
    """Same fence/commentary stripping pattern used in signals.py."""
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


async def _run_extraction(prompt_template: str, subjects_str: str, messages: list) -> Optional[Dict]:
    if not messages:
        return None
    prompt = prompt_template.format(
        subjects=subjects_str,
        messages=_format_messages(messages),
    )
    try:
        client = AsyncAnthropic()
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        parsed = _strip_and_parse(raw)
        if parsed and parsed.get("found") and parsed.get("excerpt"):
            return parsed
        return None
    except Exception as e:
        logger.error(f"[life_log] Extraction failed: {e}", exc_info=True)
        return None


async def extract_life_log_topic(circle_id: str, as_of: Optional[datetime] = None) -> Optional[Dict]:
    """
    Two-level extraction: recent unresolved thread (14-day window) first,
    then durable personal detail (full history) if nothing recent is found.

    as_of: reference time to evaluate against instead of the real current
    time. Both extraction windows are bounded by as_of on the upper end too
    (via get_messages' end_date) — otherwise, testing against a simulated
    past "now" would still leak in real messages sent after that point. See
    main_engagement.py --as-of. Defaults to real now().

    Returns {"excerpt": ..., "subject_name": ..., "source": "recent"|"durable"}
    or None. Callers must treat None as "send nothing this cycle" — never
    substitute a generic prompt.
    """
    reference_time = as_of or datetime.now(timezone.utc)

    seniors = repo.get_seniors_in_circle(circle_id)
    if not seniors:
        logger.info(f"[life_log] No seniors in circle {circle_id} — skipping extraction.")
        return None
    subjects_str = ", ".join(s["name"] for s in seniors)

    # Level 1: recent unresolved thread, bounded window (time-sensitive content).
    since = reference_time - timedelta(days=RECENT_WINDOW_DAYS)
    recent_messages = [
        m for m in repo.get_messages(circle_id, start_date=since, end_date=reference_time)
        if m.get("direction") == "inbound"
    ]
    result = await _run_extraction(RECENT_THREAD_PROMPT, subjects_str, recent_messages)
    if result:
        logger.info(
            f"[life_log] circle {circle_id}: recent-thread extraction found something "
            f"({len(recent_messages)} messages in the {RECENT_WINDOW_DAYS}-day window)."
        )
        return {"excerpt": result["excerpt"], "subject_name": result.get("subject_name"), "source": "recent"}
    logger.info(
        f"[life_log] circle {circle_id}: recent-thread extraction found nothing "
        f"({len(recent_messages)} messages in the {RECENT_WINDOW_DAYS}-day window) — trying durable detail."
    )

    # Level 2: durable personal detail, unbounded start (evergreen content
    # doesn't go stale) but still capped at reference_time on the upper end.
    all_messages = [
        m for m in repo.get_messages(circle_id, end_date=reference_time)
        if m.get("direction") == "inbound"
    ]
    result = await _run_extraction(DURABLE_DETAIL_PROMPT, subjects_str, all_messages)
    if result:
        logger.info(
            f"[life_log] circle {circle_id}: durable-detail extraction found something "
            f"({len(all_messages)} messages total)."
        )
        return {"excerpt": result["excerpt"], "subject_name": result.get("subject_name"), "source": "durable"}
    logger.info(
        f"[life_log] circle {circle_id}: durable-detail extraction also found nothing "
        f"({len(all_messages)} messages total) — nothing to send this cycle."
    )

    return None
