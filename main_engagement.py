"""
main_engagement.py — Daily engagement cron job.

For each active circle, runs a priority-ordered list of checks and sends at
most one proactive message per circle per day — whichever check fires first.

Currently wired: check 2 (pending clinical signal corroboration), ask-once —
first eligible signal gets asked about, corroboration_requested_at gets
stamped, no re-nudging, no resolution tracking. Additional checks slot into
CHECKS in priority order as they're built.

Render cron schedule: TBD
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from dotenv import load_dotenv

from take_five.integrations.groupme import send_message
from take_five.messages import ask_with_tools
from take_five.repository import repo

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def already_sent_check_in_today(circle_id: str) -> bool:
    """Guards against double-sends if the cron runs more than once in a day."""
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    messages = repo.get_messages(circle_id, start_date=start_of_day)
    return any(m["message_type"] == "check_in" for m in messages)


# ---------------------------------------------------------------------------
# Checks — each takes a circle dict, returns a hit dict or None.
# Priority order = order in CHECKS below.
# ---------------------------------------------------------------------------

def check_pending_corroboration(circle: Dict) -> Optional[Dict]:
    """
    Check 2: pending clinical signal corroboration.
    Ask-once — oldest eligible signal, no re-nudging.
    """
    signals = repo.get_pending_corroboration_signals(str(circle["id"]))
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
        "ongoing. Keep it brief and low-pressure — this is a gentle check, not an alarm."
    )
    return {"check": "pending_corroboration", "signal_id": signal["id"], "prompt": prompt}


CHECKS = [
    check_pending_corroboration,
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_circle(circle: Dict, dry_run: bool, force: bool = False) -> None:
    circle_name = circle["name"]
    ext_id = circle.get("external_id")
    bot_id = (circle.get("integration_config") or {}).get("groupme_bot_id")

    if not ext_id or not bot_id:
        logger.warning(f"Skipping {circle_name} — missing external_id or groupme_bot_id.")
        return

    if force:
        logger.warning(f"{circle_name}: --force set, skipping the one-per-day guard.")
    elif already_sent_check_in_today(str(circle["id"])):
        logger.info(f"Skipping {circle_name} — already sent a check-in today.")
        return

    hit = None
    for check in CHECKS:
        hit = check(circle)
        if hit:
            break

    if not hit:
        logger.info(f"Nothing pending for {circle_name}.")
        return

    logger.info(f"{circle_name}: firing '{hit['check']}' (signal {hit['signal_id']})")

    reply = await ask_with_tools(
        question=hit["prompt"],
        circle_id=str(circle["id"]),
        response_format="text",
        channel="groupme",
    )

    if dry_run:
        logger.info(f"[DRY RUN] {circle_name} would receive:\n{reply}")
        logger.info("[DRY RUN] Not sending to GroupMe, not writing to the DB.")
        return

    send_message(bot_id, reply)
    repo.log_message(
        circle_ext_id=ext_id,
        person_ext_id=None,
        body=reply,
        msg_type="check_in",
        direction="outbound",
        channel="groupme",
    )

    if hit["check"] == "pending_corroboration":
        repo.mark_corroboration_requested(hit["signal_id"])

    logger.info(f"Sent check-in to {circle_name}: {reply[:120]}")


async def main():
    parser = argparse.ArgumentParser(description="Run the daily engagement check-in.")
    parser.add_argument("--circle-id", dest="circle_id", default=None,
                         help="Internal UUID of a single care circle to process. Omit to process all active circles.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Log what would be sent without sending to GroupMe or writing to the DB.")
    parser.add_argument("--force", action="store_true",
                         help="Skip the one-check-in-per-day-per-circle guard. Testing only — "
                              "never pass this on a real cron run.")
    args = parser.parse_args()

    if args.circle_id:
        circle = repo.get_circle_by_id(args.circle_id)
        if not circle:
            logger.error(f"No circle found with id {args.circle_id}.")
            return
        circles: List[Dict] = [circle]
    else:
        circles = repo.get_active_circles()

    if not circles:
        logger.info("No active circles found.")
        return

    flags = f"{' [DRY RUN]' if args.dry_run else ''}{' [FORCE]' if args.force else ''}"
    logger.info(f"Found {len(circles)} active circle(s).{flags}")

    for circle in circles:
        try:
            await run_circle(circle, dry_run=args.dry_run, force=args.force)
        except Exception as e:
            logger.error(f"Failed for {circle.get('name')}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
