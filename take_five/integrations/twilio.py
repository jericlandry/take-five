import asyncio
import logging

from anthropic import AsyncAnthropic
from fastapi import Response
from twilio.twiml.messaging_response import MessagingResponse
from typing import Optional

from take_five.repository import repo
from take_five.memory import process_message_for_memory
from take_five.images import extract_sms_image, handle_image_message
from take_five.integrations.groupme import groupme_reply

logger = logging.getLogger(__name__)

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

    asyncio.create_task(process_message_for_memory(
        message_id=str(new_msg['id']),
        circle_id=str(new_msg['circle_id']),
        body=Body,
        sender=person['name'],
        sent_at=new_msg['sent_at'],
        repo=repo
    ))

    # Synthesize and post to GroupMe
    bot_id         = (circle.get('integration_config') or {}).get('groupme_bot_id')
    groupme_ext_id = circle.get('external_id')  # groupme:{group_id}

    if bot_id and groupme_ext_id:
        async def post_caregiver_update():
            try:
                client = AsyncAnthropic()
                result = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system=(
                        "You summarize caregiver check-in messages for a family care circle. "
                        "Write a single short paragraph (2-3 sentences max). "
                        "Be warm and specific — include what the senior did, how they seemed, "
                        "and anything worth knowing. No bullet points. No greeting. "
                        "Do not invent details not present in the message."
                    ),
                    messages=[{
                        "role": "user",
                        "content": f"{person['name']} checked in: {Body}"
                    }]
                )
                summary = result.content[0].text.strip()
                await groupme_reply(
                    bot_id,
                    f"{person['name']} (via Take Five): {summary}",
                    groupme_ext_id
                )
                logger.info(f"[sms] Caregiver update posted to GroupMe for circle {circle['name']}")
            except Exception as e:
                logger.error(f"[sms] Failed to synthesize or post caregiver update: {e}")
        asyncio.create_task(post_caregiver_update())

    # MMS image detection
    if int(NumMedia) > 0:
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
                if result:
                    _reply, _vision_result = result
                    # SMS reply and logging TBD when SMS channel is active
                    logger.info(f"[sms] Image processed — classification: {_vision_result.get('classification')}")
            asyncio.create_task(process_sms_image())

    logger.info(f"Twilio SMS logged from {person['name']}: '{Body}'")
    response.message(f"Got it, {person['name']}. Thanks for the update.")
    return Response(content=str(response), media_type="application/xml")
