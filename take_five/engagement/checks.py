"""
take_five/engagement/checks.py — Priority-ordered checks for the daily
engagement cron.

Each check takes a circle dict and returns a hit dict
{"check": ..., "signal_id": ..., "prompt": ...} or None. Priority order =
order in CHECKS below; take_five/engagement/runner.py takes the first
non-None result and stops — later checks are never even called once an
earlier one fires.

  1. check_pending_corroboration — pending clinical signal corroboration,
     ask-once, no re-nudging, no resolution tracking.
  2. check_life_log_gap — only reached when (1) has nothing pending. Fires
     only once the circle has gone quiet long enough (5-7 day engagement gap)
     AND a specific, grounded detail can be extracted from chat history. No
     generic fallback — if neither condition holds, nothing is sent this cycle.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from take_five.engagement.life_log import extract_life_log_topic
from take_five.repository import repo

logger = logging.getLogger(__name__)


def check_pending_corroboration(circle: Dict, as_of: Optional[datetime] = None) -> Optional[Dict]:
    """
    Tier 1: pending clinical signal corroboration.
    Ask-once — oldest eligible signal, no re-nudging.

    as_of: evaluate as if this were the current time instead of the real
    current time (see main_engagement.py --as-of). Defaults to real now().
    """
    signals = repo.get_pending_corroboration_signals(str(circle["id"]), as_of=as_of)
    if not signals:
        return None

    signal = signals[0]
    subject = signal.get("subject_name") or "them"
    prompt = (
        "You're proactively raising something with the family, not responding to a "
        "question they asked — write it in your own natural voice, don't just repeat "
        "this verbatim.\n\n"
        f"Someone mentioned this about {subject}: \"{signal['raw_excerpt']}\" "
        f"(category: {signal['signal_category']} / {signal['signal_type']}).\n\n"
        "Ask the circle to confirm whether this is accurate and whether it's new or "
        "ongoing. Keep it brief and low-pressure — this is a gentle check, not an alarm.\n\n"
        "Ground this strictly in the excerpt and subject given above. Do not pull in, "
        "reference, or attribute any other name, day, or quote from the wider "
        "conversation history — even if something else seems related. If you're not "
        "certain a detail came from this specific excerpt, leave it out."
    )
    return {"check": "pending_corroboration", "signal_id": signal["id"], "prompt": prompt}


LIFE_LOG_GAP_DAYS = 5  # lower bound of the agreed 5-7 day range; tune once live


async def check_life_log_gap(circle: Dict, as_of: Optional[datetime] = None) -> Optional[Dict]:
    """
    Tier 2: Life Log elicitation. Only reached when check_pending_corroboration
    (above) returns None — priority order = CHECKS order, signal always wins.

    Gap = days since the more recent of (a) any inbound circle-member message,
    or (b) any prior check-in (this check or corroboration) — does NOT include
    the weekly digest. Below the threshold, there's nothing to do here yet.

    as_of: evaluate as if this were the current time instead of the real
    current time — lets this be tested against a simulated "now" without
    waiting a real 5-7 days for a gap to occur (see main_engagement.py
    --as-of). Defaults to real now().

    Content is extracted from actual chat history (take_five/engagement/life_log.py) —
    a recent unresolved thread first, a durable personal detail otherwise. If
    extraction finds nothing grounded, returns None: silence, not a generic
    prompt.
    """
    reference_time = as_of or datetime.now(timezone.utc)
    circle_id = str(circle["id"])
    circle_name = circle.get("name", circle_id)
    last_activity = repo.get_last_engagement_activity(circle_id, as_of=as_of)
    if last_activity:
        gap_days = (reference_time - last_activity).days
        if gap_days < LIFE_LOG_GAP_DAYS:
            logger.info(
                f"[life_log] {circle_name}: gap is {gap_days}d, below the "
                f"{LIFE_LOG_GAP_DAYS}d threshold — not eligible yet."
            )
            return None
        logger.info(f"[life_log] {circle_name}: gap is {gap_days}d, threshold met — checking extraction.")
    else:
        logger.info(f"[life_log] {circle_name}: no prior activity found — checking extraction.")

    topic = await extract_life_log_topic(circle_id, as_of=as_of)
    if not topic:
        logger.info(f"[life_log] {circle_name}: gap met but nothing extractable from chat history.")
        return None

    logger.info(f"[life_log] {circle_name}: extracted a '{topic.get('source')}' topic.")
    subject = topic.get("subject_name") or "them"
    prompt = (
        "You're proactively raising something with the family, not responding to a "
        "question they asked — write it in your own natural voice, don't just repeat "
        "this verbatim. Keep it warm, brief, and low-pressure — a genuine check-in, "
        "not a task.\n\n"
        f"Ask the circle about this, grounded in something actually mentioned about "
        f"{subject}: \"{topic['excerpt']}\".\n\n"
        "Ground this strictly in the detail given above. Do not invent or add any "
        "other specific name, date, or quote beyond what's given here."
    )
    return {"check": "life_log", "signal_id": None, "prompt": prompt}


CHECKS = [
    check_pending_corroboration,
    check_life_log_gap,
]

# Name -> check function, for isolating a single tier during manual testing
# (see main_engagement.py --check). Keys are what you pass on the CLI.
CHECK_REGISTRY = {
    "corroboration": check_pending_corroboration,
    "life_log": check_life_log_gap,
}
