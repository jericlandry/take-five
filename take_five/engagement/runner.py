"""
take_five/engagement/runner.py — Per-circle orchestration for the daily
engagement cron.

Runs CHECKS (take_five/engagement/checks.py) in priority order and sends at
most one proactive message per circle per day — whichever check fires first.
"""

import inspect
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from take_five.engagement.checks import CHECKS
from take_five.integrations.groupme import send_message
from take_five.messages import ask_with_tools
from take_five.repository import repo

logger = logging.getLogger(__name__)


def already_sent_check_in_today(circle_id: str, as_of: Optional[datetime] = None) -> bool:
    """
    Guards against double-sends if the cron runs more than once in a day.

    as_of: evaluate "today" relative to this instead of the real current time,
    so manual testing against a simulated date (--as-of) checks the right day
    boundary instead of the real one. Defaults to real now().
    """
    reference_time = as_of or datetime.now(timezone.utc)
    start_of_day = reference_time.replace(hour=0, minute=0, second=0, microsecond=0)
    messages = repo.get_messages(circle_id, start_date=start_of_day, end_date=reference_time)
    return any(m["message_type"] == "check_in" for m in messages)


async def run_circle(circle: Dict, dry_run: bool, force: bool = False,
                      as_of: Optional[datetime] = None,
                      checks: Optional[List] = None) -> None:
    """
    as_of: simulate a different "now" for every time-dependent check —
    gap thresholds, lookback windows, the one-per-day guard — instead of the
    real current time. Lets a specific tier be tested against a specific
    point in time without waiting for real time to pass (main_engagement.py
    --as-of).

    checks: override CHECKS with a subset (e.g. just one tier) for isolated
    manual testing (main_engagement.py --check). Defaults to the full
    priority-ordered CHECKS list.
    """
    checks = checks if checks is not None else CHECKS
    circle_name = circle["name"]
    ext_id = circle.get("external_id")
    bot_id = (circle.get("integration_config") or {}).get("groupme_bot_id")

    if not ext_id or not bot_id:
        logger.warning(f"Skipping {circle_name} — missing external_id or groupme_bot_id.")
        return

    if force:
        logger.warning(f"{circle_name}: --force set, skipping the one-per-day guard.")
    elif already_sent_check_in_today(str(circle["id"]), as_of=as_of):
        logger.info(f"Skipping {circle_name} — already sent a check-in today.")
        return

    hit = None
    for check in checks:
        result = check(circle, as_of=as_of)
        if inspect.isawaitable(result):
            result = await result
        if result:
            hit = result
            break

    if not hit:
        logger.info(f"Nothing pending for {circle_name}.")
        return

    sig_label = hit["signal_id"] or "n/a"
    logger.info(f"{circle_name}: firing '{hit['check']}' (signal {sig_label})")

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
