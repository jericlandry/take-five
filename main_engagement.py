"""
main_engagement.py — Daily engagement cron job entrypoint.

Thin entrypoint only — all logic lives in take_five/engagement/:
  checks.py  — priority-ordered checks (clinical signal corroboration, Life Log)
  life_log.py — Life Log content extraction
  runner.py  — per-circle orchestration (run_circle)

Render cron schedule: 0 18 * * * (18:00 UTC / 1pm CT)
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv

from take_five.engagement.checks import CHECK_REGISTRY
from take_five.engagement.runner import run_circle
from take_five.repository import repo

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_as_of(value: str) -> datetime:
    """Accepts ISO 8601, with or without a trailing 'Z'. Assumes UTC if no
    offset is given."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def main():
    parser = argparse.ArgumentParser(description="Run the daily engagement check-in.")
    parser.add_argument("--circle-id", dest="circle_id", default=None,
                         help="Internal UUID of a single care circle to process. Omit to process all active circles.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Log what would be sent without sending to GroupMe or writing to the DB.")
    parser.add_argument("--force", action="store_true",
                         help="Skip the one-check-in-per-day-per-circle guard. Testing only — "
                              "never pass this on a real cron run.")
    parser.add_argument("--check", dest="check", default=None, choices=list(CHECK_REGISTRY.keys()),
                         help="Run only this one tier, isolated from the others (e.g. 'life_log' to "
                              "test Tier 2 even if a clinical signal is pending). Testing only — "
                              "never pass this on a real cron run.")
    parser.add_argument("--as-of", dest="as_of", default=None, type=_parse_as_of,
                         help="Evaluate every time-dependent check (gap thresholds, lookback windows, "
                              "the one-per-day guard) as if this were the current time, e.g. "
                              "'2026-07-20T18:00:00Z'. Lets a tier be tested against a simulated point "
                              "in time without waiting for real time to pass. Testing only.")
    args = parser.parse_args()

    if args.circle_id:
        circle = repo.get_circle_by_id(args.circle_id)
        if not circle:
            logger.error(f"No circle found with id {args.circle_id}.")
            return
        circles = [circle]
    else:
        circles = repo.get_active_circles()

    if not circles:
        logger.info("No active circles found.")
        return

    checks = [CHECK_REGISTRY[args.check]] if args.check else None

    flags = (
        f"{' [DRY RUN]' if args.dry_run else ''}"
        f"{' [FORCE]' if args.force else ''}"
        f"{f' [CHECK={args.check}]' if args.check else ''}"
        f"{f' [AS-OF={args.as_of.isoformat()}]' if args.as_of else ''}"
    )
    logger.info(f"Found {len(circles)} active circle(s).{flags}")

    for circle in circles:
        try:
            await run_circle(circle, dry_run=args.dry_run, force=args.force,
                              as_of=args.as_of, checks=checks)
        except Exception as e:
            logger.error(f"Failed for {circle.get('name')}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
