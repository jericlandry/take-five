"""
@T5 command handling, shared by every channel a circle can talk on.

Moved verbatim out of groupme.py's handle_groupme_webhook (Nov 2026) so the
native group-text channel (twilio_conversations.py) gets exactly the same
behavior: prep packet requests, prep packet follow-ups ("add to prep ..."),
and general questions via ask_with_tools(). The detection rules here took
several production fixes to get right (see the 2026-08-01 Addams thread
notes below), so change them here, once, for all channels.

Each channel supplies a Replier:
  reply(text): post to the circle AND log it as an outbound agent_note
               (GroupMe: groupme_reply)
  post(text):  post to the circle without logging
               (GroupMe: send_message_async; used for prep packets, which
               generate_prep_packet already logs as their own message type)
"""
import asyncio
import logging
import re
from typing import Awaitable, Protocol

from take_five.repository import repo
from take_five.messages import (
    ask_with_tools,
    generate_prep_packet,
    parse_prep_request,
    resolve_prep_seniors,
)

logger = logging.getLogger(__name__)

_T5_RE = re.compile(r'@T5', re.IGNORECASE)


class Replier(Protocol):
    async def reply(self, text: str) -> None: ...
    async def post(self, text: str) -> None: ...


def is_t5_command(text: str) -> bool:
    return bool(text and _T5_RE.search(text))


async def handle_t5(
    question: str,
    circle_id: str,
    sender_person_id,
    channel: str,
    replier: Replier,
) -> None:
    """Route an @T5 message. Caller has already checked that question is
    non-empty and circle_id resolved. Prep-packet paths run as background
    tasks (same as before the move); a general question is answered inline."""
    # Detect prep packet trigger
    question_lower = question.lower()
    is_prep_trigger = any(phrase in question_lower for phrase in [
        "prep for", "prep ", "pre-visit", "appointment prep",
        "visit prep", "get ready for",
    ]) and any(kw in question_lower for kw in [
        "appointment", "appt", "visit", "dr.", "dr ", "doctor",
    ])

    # Follow-ups on an *existing* prep packet — "add to prep [note]"
    # or asking to see/resend the updated pack. These don't carry
    # doctor/appointment context themselves, so they can't go
    # through parse_prep_request() like a fresh is_prep_trigger
    # request would (Haiku would just fall back to "the doctor").
    # Instead this looks up the most recent prep_packet message for
    # the circle (repo.get_prep_packets) and reuses its
    # doctor_name/appointment_desc/senior_person_id to regenerate.
    # A full regen naturally re-scans the message window and picks
    # up whatever was just said (e.g. "add to prep, inquire about a
    # STEADI evaluation"), so this is a full repost, not an
    # incremental patch — see 2026-08-01 Addams thread, where
    # "add to prep"/"show me the updated prep pack" fell through to
    # the general ask_with_tools() path, which has no tool for this
    # and just confabulated a confirmation without changing anything.
    # Checked before is_prep_trigger so "add to prep" (which also
    # contains the substring "prep ") doesn't get misrouted into the
    # fresh-request path with no doctor context.
    is_prep_followup = "add to prep" in question_lower or (
        ("prep pack" in question_lower or "prep packet" in question_lower)
        and any(kw in question_lower for kw in [
            "show", "see", "updated", "latest", "again", "resend", "current",
        ])
    )

    if is_prep_followup:
        logger.info(f"[{channel}] Prep packet follow-up trigger detected")
        async def run_prep_followup():
            try:
                roster = repo.fetch_circle_roster(circle_id)
                seniors = [r for r in roster if r.get("person_role") == "care_recipient"]

                packets = repo.get_prep_packets(circle_id, limit=20)
                if not packets:
                    await replier.reply("I don't have a prep packet started for this circle yet — "
                        "send @T5 prep for [name]'s appointment with [doctor] to start one.")
                    return

                # If the message names a specific senior, prefer that
                # senior's most recent packet; otherwise fall back to
                # the most recent packet overall (get_prep_packets is
                # already newest-first).
                target_senior_id = None
                if len(seniors) > 1:
                    matched = resolve_prep_seniors(question, seniors)
                    if len(matched) == 1:
                        target_senior_id = str(matched[0]["id"])

                packet_row = None
                if target_senior_id:
                    packet_row = next(
                        (p for p in packets
                         if (p.get("raw") or {}).get("senior_person_id") == target_senior_id),
                        None,
                    )
                if packet_row is None:
                    packet_row = packets[0]

                raw = packet_row.get("raw") or {}
                doctor_name       = raw.get("doctor_name") or "the doctor"
                appointment_desc  = raw.get("appointment_desc") or "upcoming appointment"
                senior_person_id  = raw.get("senior_person_id")

                packet_text, followup_text = await generate_prep_packet(
                    question=question,
                    circle_id=circle_id,
                    sender_person_id=sender_person_id,
                    doctor_name=doctor_name,
                    appointment_desc=appointment_desc,
                    senior_person_id=senior_person_id,
                )
                await replier.post(packet_text)
                await asyncio.sleep(1.5)
                await replier.reply(followup_text)
            except Exception as e:
                logger.error(f"[{channel}] Prep packet follow-up failed: {e}", exc_info=True)
                await replier.reply("Sorry, I ran into a problem updating the prep packet. Try again or ask @T5 directly.")
        asyncio.create_task(run_prep_followup())
    elif is_prep_trigger:
        logger.info(f"[{channel}] Prep packet trigger detected")
        async def run_prep():
            try:
                # Figure out which senior(s) this request is for before
                # generating anything. Circles with more than one senior
                # (e.g. a couple sharing a care circle) need this resolved
                # explicitly — see resolve_prep_seniors in
                # take_five/messages.py. This matches directly against the
                # roster, deliberately without going through the LLM, so a
                # malformed model response can't silently misroute a
                # medication-adjacent document to the wrong person.
                roster = repo.fetch_circle_roster(circle_id)
                seniors = [r for r in roster if r.get("person_role") == "care_recipient"]

                if not seniors:
                    # No senior on record at all — let generate_prep_packet's
                    # own "Mom" fallback handle it, same as before this fix.
                    target_seniors = [{"id": None, "member_name": "Mom"}]
                elif len(seniors) == 1:
                    target_seniors = seniors
                else:
                    target_seniors = resolve_prep_seniors(question, seniors)
                    if not target_seniors:
                        names = " or ".join(s["member_name"] for s in seniors)
                        await replier.reply(f"Prep pack for {names}? Send @T5 prep for [name]'s "
                            f"appointment with the doctor/appointment details and I'll put it together.")
                        return

                # Parse doctor/appointment once and reuse it across every
                # senior's packet (avoids a duplicate Haiku call per senior
                # for the "mom and dad, same appointment" case).
                parsed = await parse_prep_request(question, circle_id=circle_id)
                doctor_name = parsed["doctor_name"]
                appointment_desc = parsed["appointment_desc"]

                for i, senior in enumerate(target_seniors):
                    packet_text, followup_text = await generate_prep_packet(
                        question=question,
                        circle_id=circle_id,
                        sender_person_id=sender_person_id,
                        doctor_name=doctor_name,
                        appointment_desc=appointment_desc,
                        senior_person_id=str(senior["id"]) if senior["id"] else None,
                    )
                    await replier.post(packet_text)
                    await asyncio.sleep(1.5)
                    await replier.reply(followup_text)
                    if i < len(target_seniors) - 1:
                        await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"[{channel}] Prep packet failed: {e}", exc_info=True)
                await replier.reply("Sorry, I ran into a problem generating the prep packet. Try again or ask @T5 directly.")
        asyncio.create_task(run_prep())
    else:
        logger.info("T5 question command detected, generating response...")
        bot_response = await ask_with_tools(
            question=question,
            circle_id=circle_id,
            response_format="text",
            channel=channel,
            confirmed_by_person_id=sender_person_id,
        )
        await replier.reply(bot_response)
