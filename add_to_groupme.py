"""
add_to_groupme.py

Manual one-off utility to add a single person to a circle's GroupMe group,
by calling the real add_person_to_groupme() function directly — the same
code path the (not-yet-built) admin UI button will eventually call. Useful
for testing that function for real, and for manual adds until the UI
exists.

Usage:
    python add_to_groupme.py --circle-id <uuid> --person-id <uuid>
"""

import argparse
import asyncio
import logging

from dotenv import load_dotenv

from take_five.integrations.groupme import add_person_to_groupme

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def main(circle_id: str, person_id: str):
    try:
        result = await add_person_to_groupme(circle_id, person_id)
        logger.info(f"Success: {result}")
    except (ValueError, RuntimeError) as e:
        logger.error(f"Failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add one person to a circle's GroupMe group")
    parser.add_argument("--circle-id", required=True, help="Circle UUID")
    parser.add_argument("--person-id", required=True, help="Person UUID")
    args = parser.parse_args()

    asyncio.run(main(args.circle_id, args.person_id))
