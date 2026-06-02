"""
main_summary.py — Weekly digest cron job.

Fetches all active care circles, generates a digest for each,
and sends it to the circle's GroupMe group via its bot.

Render cron schedule: 0 22 * * 0  (Sundays at 10pm UTC)
"""

import argparse
import logging
from dotenv import load_dotenv

from take_five.integrations.groupme import send_message
from take_five.repository import repo
from take_five.summaries import generate_weekly_digest

load_dotenv()

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Generate and send weekly care circle digests.")
    parser.add_argument("--circle-id", dest="circle_id", default=None, help="Internal UUID of a single care circle to process. Omit to process all active circles.")
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
        logger.info("No active circles found. Nothing to send.")
        return

    logger.info(f"Found {len(circles)} active circle(s). Generating digests...")

    for circle in circles:
        circle_name = circle["name"]
        ext_id      = circle.get("external_id")
        bot_id      = (circle.get("integration_config") or {}).get("groupme_bot_id")

        logger.info(f"Processing: {circle_name}")

        if not ext_id:
            logger.warning(f"Skipping {circle_name} — no external_id.")
            continue

        if not bot_id:
            logger.warning(f"Skipping {circle_name} — no groupme_bot_id in integration_config.")
            continue

        response_format = "text" if ext_id.startswith("groupme:") else "markdown"

        try:
            digest = generate_weekly_digest(str(circle["id"]), response_format=response_format)
            send_message(bot_id, digest)
            repo.log_message(
                circle_ext_id=ext_id,
                person_ext_id=None,
                body=digest,
                msg_type='digest',
                direction='outbound',
                channel='groupme',
            )
            logger.info(f"Digest logged for {circle_name}")
        except Exception as e:
            logger.error(f"Failed for {circle_name}: {e}")


if __name__ == "__main__":
    main()