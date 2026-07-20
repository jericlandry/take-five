import asyncio
import logging
import os
import time

import httpx
from fastapi import Response
from twilio.twiml.messaging_response import MessagingResponse
from typing import Optional

from take_five.repository import repo
from take_five.pipeline import run_post_storage_pipeline
from take_five.images import extract_sms_image, handle_image_message
from take_five.integrations.groupme import groupme_reply, upload_image_to_groupme

logger = logging.getLogger(__name__)

# ─── SMS DISAMBIGUATION (single shared Twilio number) ───
#
# Take Five uses one Twilio number for the whole platform, so a message is
# identified by who's texting (From), not which number they texted (To).
# That's normally unambiguous, but a phone can be sms_active in more than
# one circle (e.g. a tester playing multiple roles). When that happens we
# hold the message and ask which circle it's for.
#
# This is in-memory, not a DB table: it's a short-lived (15 min), low-volume
# edge case, and the project already prefers existing context over new state
# tables where possible. It won't survive a process restart or multiple
# workers — acceptable for now given how rarely it fires, but worth
# revisiting if the platform moves to multiple app instances.
_PENDING_SMS_TTL_SECONDS = 15 * 60
_pending_sms_disambiguation: dict[str, dict] = {}


def _stash_pending_disambiguation(phone: str, candidates: list, payload: dict) -> None:
    _pending_sms_disambiguation[phone] = {
        'candidates': candidates,
        'payload': payload,
        'expires_at': time.time() + _PENDING_SMS_TTL_SECONDS,
    }


def _get_pending_disambiguation(phone: str) -> Optional[dict]:
    pending = _pending_sms_disambiguation.get(phone)
    if not pending:
        return None
    if time.time() > pending['expires_at']:
        del _pending_sms_disambiguation[phone]
        return None
    return pending


def _match_circle_reply(reply: str, candidates: list) -> Optional[dict]:
    """Match a disambiguation reply against candidate circles by number,
    circle name, or ensemble name."""
    reply_clean = reply.strip().lower()
    if reply_clean.isdigit():
        idx = int(reply_clean) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    name_matches = [
        c for c in candidates
        if reply_clean in c['circle_name'].lower() or c['circle_name'].lower() in reply_clean
        or reply_clean in c['ensemble_name'].lower() or c['ensemble_name'].lower() in reply_clean
    ]
    if len(name_matches) == 1:
        return name_matches[0]
    return None


def _disambiguation_prompt(candidates: list) -> str:
    options = "\n".join(
        f"{i+1}) {c['circle_name']} ({c['ensemble_name']})" for i, c in enumerate(candidates)
    )
    return f"Take Five: you're in more than one care circle. Reply with the number this is for:\n{options}"


def _row_to_person_and_circle(row: dict) -> tuple:
    """Split a find_active_sms_members_by_phone row into (person, circle) dicts
    matching the shapes the rest of this module expects."""
    circle = {
        'id': row['circle_id'],
        'name': row['circle_name'],
        'ensemble_name': row['ensemble_name'],
        'external_id': row['circle_external_id'],
        'integration_config': row['circle_integration_config'],
        'status': 'active',  # query already filters to active circles
    }
    person = {k: v for k, v in row.items() if not k.startswith('circle_') and k != 'ensemble_name'}
    return person, circle


async def fetch_twilio_media(url: str) -> Optional[tuple[bytes, str]]:
    """
    Fetch MMS media bytes from Twilio. Twilio media URLs are protected and
    require Basic Auth with the account SID/token — an unauthenticated fetch
    returns 401. Returns (bytes, content_type) or None on failure.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.error("[sms] TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set — cannot fetch media")
        return None

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url, auth=(account_sid, auth_token))

    if response.status_code != 200:
        logger.error(f"[sms] Failed to fetch media from Twilio: {response.status_code} - {response.text}")
        return None

    content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    return response.content, content_type

async def handle_sms(
    From: str,
    Body: str,
    To: str,
    NumMedia: str = "0",
    MediaUrl0: Optional[str] = None,
    MediaContentType0: Optional[str] = None,
) -> Response:
    response = MessagingResponse()
    payload = {
        "Body": Body, "To": To,
        "NumMedia": NumMedia, "MediaUrl0": MediaUrl0, "MediaContentType0": MediaContentType0,
    }

    # 0. Resolve a pending disambiguation for this phone, if any. If it's
    # there, this message is the sender's answer ("1" / a circle name),
    # not a new care update — the original held message gets processed
    # once we know which circle it belongs to.
    pending = _get_pending_disambiguation(From)
    if pending:
        matched = _match_circle_reply(Body, pending['candidates'])
        if not matched:
            response.message(_disambiguation_prompt(pending['candidates']))
            return Response(content=str(response), media_type="application/xml")
        del _pending_sms_disambiguation[From]
        person, circle = _row_to_person_and_circle(matched)
        return await _process_caregiver_sms(person, circle, From, **pending['payload'])

    # 1. Identify sender by phone alone — one shared Twilio number serves
    # every circle, so circle identity comes from who's texting, not which
    # number was texted.
    matches = repo.find_active_sms_members_by_phone(From)

    if not matches:
        logger.warning(f"SMS from unrecognized number {From}")
        response.message("We don't recognize this number. Contact your care circle administrator.")
        return Response(content=str(response), media_type="application/xml")

    if len(matches) > 1:
        logger.info(f"[sms] {From} is sms_active in {len(matches)} circles — asking which one")
        _stash_pending_disambiguation(From, matches, payload)
        response.message(_disambiguation_prompt(matches))
        return Response(content=str(response), media_type="application/xml")

    person, circle = _row_to_person_and_circle(matches[0])
    return await _process_caregiver_sms(person, circle, From, **payload)


async def _process_caregiver_sms(
    person: dict,
    circle: dict,
    From: str,
    Body: str,
    To: str,
    NumMedia: str = "0",
    MediaUrl0: Optional[str] = None,
    MediaContentType0: Optional[str] = None,
) -> Response:
    """Log, relay, and reply to a caregiver SMS once sender + circle are resolved."""
    response = MessagingResponse()
    circle_id     = str(circle['id'])
    circle_ext_id = circle['external_id']

    logger.info(f"SMS received from {person['name']} ({From}) for circle {circle['name']}")

    new_msg = repo.log_message(
        circle_ext_id=circle_ext_id,
        person_ext_id=None,
        body=Body,
        raw_data={"from": From, "to": To, "body": Body},
        channel="sms",
        person_id=str(person['id']),
    )

    asyncio.create_task(run_post_storage_pipeline(
        message_id=str(new_msg['id']),
        circle_id=str(new_msg['circle_id']),
        body=Body,
        sender=person['name'],
        sent_at=new_msg['sent_at'],
        channel="sms",
    ))

    # Relay to GroupMe — raw text as written, no LLM paraphrasing. If there's an
    # MMS image, it's re-hosted on GroupMe's Image Service and attached to the
    # same message so it posts as one combined text+photo reply, same as if the
    # sender had posted it in GroupMe directly.
    bot_id         = (circle.get('integration_config') or {}).get('groupme_bot_id')
    groupme_ext_id = circle.get('external_id')  # groupme:{group_id}
    has_media      = int(NumMedia) > 0 and bool(MediaUrl0)

    if bot_id and groupme_ext_id:
        async def relay_to_groupme():
            relay_text = (
                f"{person['name']} (via Take Five): {Body}" if Body.strip()
                else f"{person['name']} (via Take Five) shared a photo:"
            )
            picture_url = None
            if has_media:
                fetched = await fetch_twilio_media(MediaUrl0)
                if fetched:
                    image_bytes, content_type = fetched
                    picture_url = await upload_image_to_groupme(image_bytes, content_type)
                    if not picture_url:
                        logger.error("[sms] Image upload to GroupMe failed — posting text only")
                else:
                    logger.error("[sms] Could not fetch media from Twilio — posting text only")
            await groupme_reply(bot_id, relay_text, groupme_ext_id, picture_url=picture_url)
            logger.info(f"[sms] Caregiver update relayed to GroupMe for circle {circle['name']}")
        asyncio.create_task(relay_to_groupme())

    # MMS image detection — separate clinical vision pipeline (medication extraction
    # etc.), independent of the raw relay above. Mirrors handle_groupme_webhook's
    # process_image(): posts a reply when there is one (e.g. the medication
    # confirmation card) and always logs an agent_note with the vision result.
    if has_media:
        sms_payload = {
            "NumMedia": NumMedia,
            "MediaUrl0": MediaUrl0,
            "MediaContentType0": MediaContentType0,
            "Body": Body, "From": From, "To": To,
            "sender_name": person['name'], "MessageSid": "",
        }
        image_attachment = extract_sms_image(sms_payload)
        if image_attachment:
            async def process_sms_image():
                result = await handle_image_message(image_attachment)
                if not result:
                    return
                reply, vision_result = result
                if reply:
                    await groupme_reply(bot_id, reply, groupme_ext_id)

                classification = vision_result.get("classification")
                parts = [f"Image received from {image_attachment.sender_name} via SMS."]
                if image_attachment.message_text:
                    parts.append(f"Caption: \"{image_attachment.message_text}\".")

                if classification == "MEDICATION":
                    extracted = vision_result.get("extracted") or {}
                    name = extracted.get("medication_name")
                    brand = extracted.get("brand_name")
                    dosage = extracted.get("dosage", "")
                    instructions = extracted.get("instructions", "")
                    kind = "supplement" if extracted.get("is_supplement") else "medication"
                    label = f"{name}{f' ({brand})' if brand else ''}"
                    parts.append(f"Extracted: {label}, {dosage}, {kind}, {instructions}.")
                else:
                    description = vision_result.get("description", "")
                    text_found = vision_result.get("text_found")
                    if description:
                        parts.append(description)
                    if text_found:
                        parts.append(f"Text found: {text_found}.")

                repo.log_message(
                    circle_ext_id=groupme_ext_id,
                    person_ext_id=None,
                    body=" ".join(parts),
                    raw_data=vision_result,
                    msg_type="agent_note",
                    direction="outbound",
                    channel="sms",
                )
            asyncio.create_task(process_sms_image())

    logger.info(f"Twilio SMS logged from {person['name']}: '{Body}'")

    # Placeholder confirmation reply — sent via TwiML, not a separate REST call.
    # TODO: personalize based on circle/context once the pipeline supports it.
    confirmation_text = f"Got it, {person['name']}. Thanks for the update."
    logger.info(f"[sms] Sending confirmation reply to {person['name']} ({From}): '{confirmation_text}'")
    repo.log_message(
        circle_ext_id=circle_ext_id,
        person_ext_id=None,
        body=confirmation_text,
        raw_data={"from": To, "to": From, "body": confirmation_text},
        msg_type="agent_note",
        direction="outbound",
        channel="sms",
    )

    response.message(confirmation_text)
    return Response(content=str(response), media_type="application/xml")
