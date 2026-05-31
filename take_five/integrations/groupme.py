import asyncio
import logging

import httpx
from typing import Optional

from take_five.repository import repo
from take_five.memory import process_message_for_memory
from take_five.messages import ask_with_tools
from take_five.images import extract_groupme_image, handle_image_message

logger = logging.getLogger(__name__)

GROUPME_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_HEADERS = {
    "User-Agent": "curl/7.68.0",
    "Content-Type": "application/json"
}

def send_message(bot_id: str, text: str) -> bool:
    """Send a message to a GroupMe bot. Returns True on success.

    bot_id comes from care_circles.integration_config['groupme_bot_id'].
    """
    with httpx.Client() as client:
        response = client.post(
            GROUPME_URL,
            json={"bot_id": bot_id, "text": text},
            headers=GROUPME_HEADERS
        )
    if response.status_code == 202:
        logger.info("Message sent successfully to GroupMe")
        return True
    logger.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
    return False


async def send_message_async(bot_id: str, text: str) -> bool:
    """Async version for use inside the FastAPI webhook.

    bot_id comes from care_circles.integration_config['groupme_bot_id'].
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            GROUPME_URL,
            json={"bot_id": bot_id, "text": text},
            headers=GROUPME_HEADERS
        )
    if response.status_code == 202:
        logger.info("Message sent successfully to GroupMe")
        return True
    logger.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
    return False


async def groupme_reply(bot_id: Optional[str], text: Optional[str], circle_ext_id: Optional[str] = None):
    """
    Post a reply to GroupMe and log it as an outbound agent_note.
    No-op if bot_id or text is missing.

    Internal sentinels ([SAVED: ...], [PATCHED: ...]) are stripped from the
    visible GroupMe message but preserved in the logged body so Claude can
    read them in future turns for state tracking.
    """
    if not bot_id or not text:
        return
    # Strip sentinel lines before posting — users never see them
    visible_text = "\n".join(
        line for line in text.splitlines()
        if not line.startswith("[SAVED:") and not line.startswith("[PATCHED:")
    ).strip()
    await send_message_async(bot_id, visible_text)
    if circle_ext_id:
        try:
            repo.log_message(
                circle_ext_id=circle_ext_id,
                person_ext_id=None,
                body=text,
                raw_data={"source": "t5_bot", "bot_id": bot_id},
                msg_type="agent_note",
                direction="outbound",
                channel="groupme",
            )
            logger.info(f"[groupme] Bot reply logged to {circle_ext_id}")
        except Exception as e:
            logger.error(f"[groupme] Failed to log bot reply: {e}")


async def handle_groupme_webhook(data: dict):
    logger.info("GroupMe webhook received")
    logger.info(f"Webhook data: {data}")

    # 1. Guard: ignore bot's own messages to avoid infinite loops
    if data.get("sender_type") == "bot":
        logger.info("Bot message ignored")
        return {"status": "ignored"}

    # 2. Extract fields
    circle_ext_id = f"groupme:{data.get('group_id')}"
    person_ext_id = f"groupme:{data.get('sender_id')}"
    person_name   = data.get("name", "Unknown User")
    text          = data.get("text", "")

    logger.info(f"Processing message from {person_name} in group {circle_ext_id}")

    try:
        new_msg = repo.log_message(
            circle_ext_id=circle_ext_id,
            person_ext_id=person_ext_id,
            body=text,
            raw_data=data,
            channel="groupme"
        )

        asyncio.create_task(process_message_for_memory(
            message_id=str(new_msg['id']),
            circle_id=str(new_msg['circle_id']),
            body=text,
            sender=person_name,
            sent_at=new_msg['sent_at'],
            repo=repo
        ))

        # Resolve circle once — used by both image and ask branches
        circle    = repo.get_circle_by_external_id(circle_ext_id)
        circle_id = circle['id'] if circle else None
        bot_id    = (circle.get('integration_config') or {}).get('groupme_bot_id') if circle else None

        # Resolve the sender's person_id for confirmed_by tracking
        sender_person    = repo.get_person_by_external_id(person_ext_id)
        sender_person_id = str(sender_person['id']) if sender_person else None

        # 3. Image detection — returns (reply, vision_result) tuple or None
        image_attachment = extract_groupme_image(data)
        if image_attachment:
            async def process_image():
                result = await handle_image_message(image_attachment)
                if not result:
                    return
                reply, vision_result = result
                if reply:
                    await groupme_reply(bot_id, reply, circle_ext_id)

                # Build a clean log body for the messages table
                classification = vision_result.get("classification")
                caption = image_attachment.message_text
                parts = [f"Image received from {image_attachment.sender_name}."]
                if caption:
                    parts.append(f"Caption: \"{caption}\".")

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
                    circle_ext_id=circle_ext_id,
                    person_ext_id=None,
                    body=" ".join(parts),
                    raw_data=vision_result,
                    msg_type="agent_note",
                    direction="outbound",
                    channel="groupme",
                )
            asyncio.create_task(process_image())

        # 4. T5 ask flow — ask_with_tools handles both Q&A and medication saves
        if '@T5' in text:
            question = text.split('@T5', 1)[1].strip()
            if not question:
                logger.warning("T5 command detected but no question found.")
                return {"status": "ok"}
            if not circle_id:
                logger.error(f"Circle with external_id {circle_ext_id} not found.")
                return {"status": "ok"}
            if not bot_id:
                logger.error(f"No groupme_bot_id in integration_config for circle {circle_ext_id}.")
                return {"status": "ok"}

            logger.info("T5 question command detected, generating response...")
            bot_response = await ask_with_tools(
                question=question,
                circle_id=circle_id,
                response_format="text",
                confirmed_by_person_id=sender_person_id,
            )
            await groupme_reply(bot_id, bot_response, circle_ext_id)

        logger.info(f"Message stored. Internal ID: {new_msg['id']}")

    except Exception as e:
        logger.error(f"Failed to sync or log message: {e}")
        return {"status": "error", "message": str(e)}

    logger.info("Webhook processed successfully")
    return {"status": "ok"}
