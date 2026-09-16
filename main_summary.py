"""
main_summary.py — Weekly digest cron job.

Fetches all active care circles, generates a digest for each,
and sends it to the circle's GroupMe group via its bot.

Render cron schedule: 0 22 * * 0  (Sundays at 10pm UTC)
"""

import argparse
import asyncio
import logging
from dotenv import load_dotenv

from take_five.integrations.groupme import send_message
from take_five.integrations.sendgrid_email import send_email, circle_inbound_address
from take_five.repository import repo
from take_five.summaries import generate_weekly_digest, generate_outer_weekly_digest, generate_senior_digest

load_dotenv()

logger = logging.getLogger(__name__)


def send_senior_emails(circle: dict) -> None:
    """
    Send the senior-facing weekly email (see take_five/prompts/
    t5_week_summary_senior.md and generate_senior_digest()) to each
    role='senior' person in this circle who has an email on file.

    Independent of the GroupMe digest above -- runs regardless of whether
    this circle has a GroupMe bot configured, since email is its own
    channel now, not contingent on GroupMe existing. A circle with no
    seniors, or with seniors but no email on file for any of them, is a
    silent no-op -- most circles today, since this is a new, opt-in-by-
    having-an-email-on-file feature, not something every circle gets by
    default yet.
    """
    seniors = repo.get_seniors_in_circle(str(circle["id"]))
    seniors_with_email = [s for s in seniors if s.get("email")]
    if not seniors_with_email:
        return

    ensemble = repo.get_ensemble(str(circle["ensemble_id"]))
    ensemble_name = ensemble["name"] if ensemble else circle["name"]
    from_display_name = f"{ensemble_name} - {circle['name']}"
    reply_to = circle_inbound_address(str(circle["id"]))

    for senior in seniors_with_email:
        other_seniors = [s for s in seniors if s["id"] != senior["id"]]
        try:
            body = asyncio.run(generate_senior_digest(str(circle["id"]), senior, other_seniors))
            if not body:
                logger.warning(
                    f"[senior-email] No digest generated for {senior['name']} "
                    f"in {circle['name']} -- skipping send"
                )
                continue

            sent = asyncio.run(send_email(
                to_email=senior["email"],
                to_name=senior["name"],
                from_display_name=from_display_name,
                reply_to=reply_to,
                subject="This week, from Take Five",
                body_text=body,
            ))
            if not sent:
                continue

            # See module docstring above: log_message always requires
            # circle_ext_id (even via the person_id-override path), so a
            # circle with no GroupMe setup at all (external_id never set)
            # can't be logged here -- send still succeeded either way, this
            # only affects the audit-trail row. Every circle in production
            # today has GroupMe configured, so this is a dormant edge case,
            # not an active gap -- flagged rather than silently assumed
            # away, since it'll bite the first genuinely email-only circle.
            if circle.get("external_id"):
                repo.log_message(
                    circle_ext_id=circle["external_id"],
                    person_ext_id=None,
                    person_id=senior["id"],
                    body=body,
                    msg_type="senior_digest",
                    direction="outbound",
                    channel="email",
                )
            else:
                logger.warning(
                    f"[senior-email] Sent to {senior['name']} but circle "
                    f"{circle['name']} has no external_id -- could not log"
                )
            logger.info(f"[senior-email] Sent to {senior['name']} ({circle['name']})")
        except Exception as e:
            logger.error(f"[senior-email] Failed for {senior['name']} in {circle['name']}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Generate and send weekly care circle digests.")
    parser.add_argument("--circle-id", dest="circle_id", default=None, help="Internal UUID of a single care circle to process. Omit to process all active circles.")
    parser.add_argument("--email-only", dest="email_only", action="store_true", help="Send only the senior-facing email (send_senior_emails) -- skip the GroupMe family digest entirely, including its log_message call. Useful for re-sending/testing the senior email in isolation, e.g. after a prompt change, without re-posting to the family chat.")
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

        # Independent of GroupMe config below -- email is its own channel.
        send_senior_emails(circle)

        if args.email_only:
            logger.info(f"--email-only set -- skipping GroupMe digest for {circle_name}.")
            continue

        if not ext_id:
            logger.warning(f"Skipping {circle_name} — no external_id.")
            continue

        if not bot_id:
            logger.warning(f"Skipping {circle_name} — no groupme_bot_id in integration_config.")
            continue

        response_format = "text" if ext_id.startswith("groupme:") else "markdown"

        try:
            if circle.get('parent_circle_id'):
                # Outer circle -- redacted digest, reads inner circle too.
                # See t5_week_summary_outer.md and
                # summaries._contains_restricted_content for why this is a
                # separate path rather than reusing generate_weekly_digest().
                result = generate_outer_weekly_digest(str(circle["id"]), response_format=response_format)
                digest = result["digest"]
                if result["blocked"]:
                    logger.error(
                        f"[outer-digest] BLOCKED for {circle_name} -- flagged terms: "
                        f"{result['flagged_terms']}. Not posted, not logged. Manual review needed."
                    )
                    continue
            else:
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