import asyncio
import logging
import os
import re

import httpx
from typing import Optional

from take_five.repository import repo
from take_five.pipeline import run_post_storage_pipeline
from take_five.messages import ask_with_tools, generate_prep_packet
from take_five.images import extract_groupme_image, handle_image_message

logger = logging.getLogger(__name__)

GROUPME_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_HEADERS = {
    "User-Agent": "curl/7.68.0",
    "Content-Type": "application/json"
}

GROUPME_MAX_CHARS = 4000


def split_for_groupme(text: str, limit: int = GROUPME_MAX_CHARS) -> list[str]:
    """Split text into chunks at sentence boundaries, each within `limit` chars.

    Splits on '. ', '! ', '? ' followed by a capital letter or digit, which
    avoids false positives on abbreviations like 'Dr.' or 'e.g.'
    If a single sentence exceeds the limit it is hard-split at the limit.
    """
    if len(text) <= limit:
        return [text]

    # Tokenize into sentences using a regex that avoids common abbreviations
    sentence_re = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9])')
    sentences = sentence_re.split(text)

    chunks = []
    current = ""
    for sentence in sentences:
        # +1 for the space we'll add between sentences
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Single sentence longer than limit — hard split
            while len(sentence) > limit:
                chunks.append(sentence[:limit])
                sentence = sentence[limit:]
            current = sentence
    if current:
        chunks.append(current)

    return chunks


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
    Automatically splits text that exceeds GROUPME_MAX_CHARS at sentence
    boundaries and sends each chunk sequentially.
    """
    chunks = split_for_groupme(text)
    all_ok = True
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            response = await client.post(
                GROUPME_URL,
                json={"bot_id": bot_id, "text": chunk},
                headers=GROUPME_HEADERS
            )
            if response.status_code == 202:
                logger.info(f"Message chunk sent successfully to GroupMe ({len(chunk)} chars)")
            else:
                logger.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
                all_ok = False
    return all_ok


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

    # 1. Guard: ignore bot's own messages and system messages (e.g. join/leave events)
    if data.get("sender_type") in ("bot", "system"):
        logger.info(f"{data.get('sender_type')} message ignored")
        return {"status": "ignored"}

    # 2. Extract fields
    circle_ext_id = f"groupme:{data.get('group_id')}"
    person_ext_id = f"groupme:{data.get('sender_id')}"
    person_name   = data.get("name", "Unknown User")
    text          = data.get("text", "")

    logger.info(f"Processing message from {person_name} in group {circle_ext_id}")

    try:
        # Upsert person and circle membership before logging the message
        # so foreign key lookups in log_message succeed
        circle = repo.get_circle_by_external_id(circle_ext_id)
        is_new_person = False
        if circle:
            person = repo.get_person_by_external_id(person_ext_id)
            if not person:
                # Fallback: look for an existing person in this ensemble with a matching
                # name and no external_id yet (e.g. admin-created records, like Mona
                # before her first GroupMe post). Avoids creating duplicate people.
                candidate = repo._execute("""
                    SELECT id FROM people
                    WHERE ensemble_id = %(ensemble_id)s
                      AND external_id IS NULL
                      AND LOWER(name) = LOWER(%(name)s)
                    LIMIT 1;
                """, {'ensemble_id': str(circle['ensemble_id']), 'name': person_name})
                if candidate:
                    person = repo.update_person(str(candidate['id']), external_id=person_ext_id)
                    logger.info(f"[groupme] Matched existing person by name, linked external_id: {person_name} ({person_ext_id})")
                else:
                    # New person — add to the ensemble that owns this circle
                    person = repo.add_person_to_ensemble(
                        ensemble_id=str(circle['ensemble_id']),
                        name=person_name,
                        external_id=person_ext_id,
                    )
                    is_new_person = True
                    logger.info(f"[groupme] Created new person: {person_name} ({person_ext_id})")
            # Upsert membership with DO NOTHING on conflict so admin-assigned roles are preserved
            repo._execute("""
                INSERT INTO circle_memberships (circle_id, person_id, role)
                VALUES (%(circle_id)s, %(person_id)s, 'family')
                ON CONFLICT (circle_id, person_id) DO NOTHING;
            """, {'circle_id': str(circle['id']), 'person_id': str(person['id'])}, fetch=None)
        else:
            logger.warning(f"[groupme] No circle found for external_id {circle_ext_id} — skipping upsert")

        new_msg = repo.log_message(
            circle_ext_id=circle_ext_id,
            person_ext_id=person_ext_id,
            body=text,
            raw_data=data,
            channel="groupme"
        )

        asyncio.create_task(run_post_storage_pipeline(
            message_id=str(new_msg['id']),
            circle_id=str(new_msg['circle_id']),
            body=text,
            sender=person_name,
            sent_at=new_msg['sent_at'],
            channel="groupme",
        ))

        # Resolve circle once — used by both image and ask branches
        circle    = repo.get_circle_by_external_id(circle_ext_id)
        circle_id = circle['id'] if circle else None
        bot_id    = (circle.get('integration_config') or {}).get('groupme_bot_id') if circle else None

        # Send welcome message to new members
        if is_new_person and bot_id:
            asyncio.create_task(send_message_async(
                bot_id,
                f"Welcome {person_name}! I'm Take Five, your family's care assistant. I'll keep track of updates shared here and send a weekly digest to the circle. Just chat normally — I'll handle the rest."
            ))

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

            # Detect prep packet trigger
            question_lower = question.lower()
            is_prep_trigger = any(phrase in question_lower for phrase in [
                "prep for", "prep ", "pre-visit", "appointment prep",
                "visit prep", "get ready for",
            ]) and any(kw in question_lower for kw in [
                "appointment", "appt", "visit", "dr.", "dr ", "doctor",
            ])

            if is_prep_trigger:
                logger.info("[groupme] Prep packet trigger detected")
                async def run_prep():
                    try:
                        packet_text, followup_text = await generate_prep_packet(
                            question=question,
                            circle_id=circle_id,
                            sender_person_id=sender_person_id,
                        )
                        await send_message_async(bot_id, packet_text)
                        await asyncio.sleep(1.5)
                        await groupme_reply(bot_id, followup_text, circle_ext_id)
                    except Exception as e:
                        logger.error(f"[groupme] Prep packet failed: {e}", exc_info=True)
                        await groupme_reply(
                            bot_id,
                            "Sorry, I ran into a problem generating the prep packet. Try again or ask @T5 directly.",
                            circle_ext_id,
                        )
                asyncio.create_task(run_prep())
            else:
                logger.info("T5 question command detected, generating response...")
                bot_response = await ask_with_tools(
                    question=question,
                    circle_id=circle_id,
                    response_format="text",
                    channel="groupme",
                    confirmed_by_person_id=sender_person_id,
                )
                await groupme_reply(bot_id, bot_response, circle_ext_id)

        logger.info(f"Message stored. Internal ID: {new_msg['id']}")

    except Exception as e:
        logger.error(f"Failed to sync or log message: {e}")
        return {"status": "error", "message": str(e)}

    logger.info("Webhook processed successfully")
    return {"status": "ok"}


async def setup_groupme_circle(circle_id: str) -> dict:
    """
    Programmatically sets up a GroupMe group and bot for a care circle.

    Steps:
      1. Fetch the circle and its ensemble admin's phone number
      2. Create the GroupMe group
      3. Add the ensemble admin to the group by phone number
      4. Register the Take Five bot in the group
      5. Store group_id, bot_id, and external_id back on the circle record

    Returns a summary dict with group_id and bot_id.
    """
    GROUPME_API_BASE = "https://api.groupme.com/v3"
    GROUPME_ACCESS_TOKEN = os.getenv("GROUPME_USER_ACCESS_TOKEN")
    GROUPME_CALLBACK_URL = "https://app.takefive.care/groupme/webhook"
    BOT_NAME = "Take Five"

    if not GROUPME_ACCESS_TOKEN:
        raise ValueError("GROUPME_USER_ACCESS_TOKEN not set in environment")

    # 1. Fetch the circle
    circle = repo.get_circle_by_id(circle_id)
    if not circle:
        raise ValueError(f"Circle {circle_id} not found")
    circle_name = circle['name']
    ensemble_id = str(circle['ensemble_id'])

    # 2. Find the ensemble admin's phone number
    admin = repo._execute("""
        SELECT p.phone, p.name
        FROM people p
        JOIN ensemble_memberships em ON em.person_id = p.id
        WHERE em.ensemble_id = %(ensemble_id)s
          AND em.user_role = 'admin'
        LIMIT 1;
    """, {'ensemble_id': ensemble_id})

    if not admin:
        raise ValueError(f"No admin found for ensemble {ensemble_id}")
    if not admin['phone']:
        raise ValueError(f"Admin '{admin['name']}' has no phone number on record")

    async with httpx.AsyncClient() as client:
        # 3. Create the GroupMe group
        group_resp = await client.post(
            f"{GROUPME_API_BASE}/groups",
            params={"token": GROUPME_ACCESS_TOKEN},
            json={"name": circle_name},
        )
        if group_resp.status_code != 201:
            raise RuntimeError(f"GroupMe group creation failed: {group_resp.status_code} {group_resp.text}")
        group_id = group_resp.json()['response']['id']
        logger.info(f"[groupme-setup] Created group '{circle_name}' with id {group_id}")

        # 4. Add the ensemble admin to the group by phone number
        # Normalize E.164 (+15127404620) to GroupMe's expected format (+1 5127404620)
        phone = admin['phone']
        if phone.startswith('+1') and len(phone) == 12:
            phone = f"+1 {phone[2:]}"
        member_resp = await client.post(
            f"{GROUPME_API_BASE}/groups/{group_id}/members/add",
            params={"token": GROUPME_ACCESS_TOKEN},
            json={"members": [{"nickname": admin['name'], "phone_number": phone}]},
        )
        if member_resp.status_code != 202:
            logger.warning(f"[groupme-setup] Member add returned {member_resp.status_code}: {member_resp.text}")
        else:
            logger.info(f"[groupme-setup] Invited {admin['name']} ({admin['phone']}) to group")

        # 5. Register the bot
        bot_resp = await client.post(
            f"{GROUPME_API_BASE}/bots",
            params={"token": GROUPME_ACCESS_TOKEN},
            json={"bot": {
                "name": BOT_NAME,
                "group_id": group_id,
                "callback_url": GROUPME_CALLBACK_URL,
            }},
        )
        if bot_resp.status_code != 201:
            raise RuntimeError(f"GroupMe bot creation failed: {bot_resp.status_code} {bot_resp.text}")
        bot_id = bot_resp.json()['response']['bot']['bot_id']
        logger.info(f"[groupme-setup] Registered bot with id {bot_id}")

    # 6. Store group_id, bot_id, and external_id on the circle record
    repo.update_care_circle(circle_id, {
        'external_id': f"groupme:{group_id}",
        'integration_config': {
            'groupme_group_id': group_id,
            'groupme_bot_id': bot_id,
        },
    })
    logger.info(f"[groupme-setup] Circle {circle_id} updated with groupme config")

    return {
        'group_id': group_id,
        'bot_id': bot_id,
        'group_name': circle_name,
        'admin_invited': admin['name'],
    }

