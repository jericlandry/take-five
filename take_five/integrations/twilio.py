import asyncio
import logging
import os

import httpx
from fastapi import Response
from twilio.twiml.messaging_response import MessagingResponse
from typing import Optional

from take_five.repository import repo
from take_five.pipeline import run_post_storage_pipeline
from take_five.images import extract_sms_image, handle_image_message
from take_five.integrations.groupme import groupme_reply, upload_image_to_groupme

logger = logging.getLogger(__name__)


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

    # 1. Identify care circle by the Twilio number that received the message
    circle = repo.get_circle_by_twilio_number(To)
    if not circle:
        logger.warning(f"SMS received on unrecognized Twilio number: {To}")
        response.message("We don't recognize this number. Contact your care circle administrator.")
        return Response(content=str(response), media_type="application/xml")

    # Status guard: archived circles are fully offboarded — no ingestion,
    # no relay. Unlike GroupMe (silent drop), the SMS sender gets a reply:
    # a caregiver texting an offboarded circle deserves to know it's closed
    # rather than wondering why their updates vanish.
    if circle.get('status') != 'active':
        logger.info(f"[sms] Circle '{circle['name']}' is {circle['status']} — rejecting inbound SMS")
        response.message("This care circle is no longer active. Contact your care circle administrator.")
        return Response(content=str(response), media_type="application/xml")

    circle_id     = str(circle['id'])
    circle_ext_id = circle['external_id']  # use the circle's real external_id for logging

    # 2. Identify sender — must be an sms_active member of this specific circle
    person = repo.find_caregiver_by_phone_and_circle(From, circle_id)

    if not person:
        logger.warning(f"SMS from unrecognized number {From} for circle {circle['name']}")
        response.message("We don't recognize this number. Contact your care circle administrator.")
        return Response(content=str(response), media_type="application/xml")

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
