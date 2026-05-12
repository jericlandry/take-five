"""
main_summary.py — Weekly digest cron job.

Fetches all active care circles, generates a digest for each,
and sends it to the circle's GroupMe group via its bot.

Render cron schedule: 0 22 * * 0  (Sundays at 10pm UTC)
"""

import logging
from dotenv import load_dotenv

from take_five.repository import TakeFiveRepository
from take_five.summaries import generate_weekly_digest
from take_five.integrations.groupme import send_message

load_dotenv()
logging.basicConfig(level=logging.INFO)


def main():
    repo = TakeFiveRepository()
    circles = repo.get_active_circles()

    if not circles:
        logging.info("No active circles found. Nothing to send.")
        return

    logging.info(f"Found {len(circles)} active circle(s). Generating digests...")

    for circle in circles:
        circle_name = circle["name"]
        ext_id      = circle.get("external_id")
        bot_id      = (circle.get("integration_config") or {}).get("groupme_bot_id")

        logging.info(f"Processing: {circle_name}")

        if not ext_id:
            logging.warning(f"Skipping {circle_name} — no external_id.")
            continue

        if not bot_id:
            logging.warning(f"Skipping {circle_name} — no groupme_bot_id in integration_config.")
            continue

        response_format = "text" if ext_id.startswith("groupme:") else "markdown"

        try:
            digest = generate_weekly_digest(str(circle["id"]), response_format=response_format)
            send_message(bot_id, digest)
        except Exception as e:
            logging.error(f"Failed for {circle_name}: {e}")


if __name__ == "__main__":
    main()