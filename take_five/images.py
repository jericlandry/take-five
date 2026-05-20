"""
take_five/images.py

Image detection and analysis pipeline for GroupMe webhook messages.
Phase 1: Vision classification and extraction — logs to console only.
Phase 2 (future): DB write via tool use after confirmation flow.
"""

import logging
import httpx
import anthropic
import json

logger = logging.getLogger(__name__)

ANTHROPIC_CLIENT = anthropic.Anthropic()

VISION_PROMPT = """You are analyzing an image sent in a family elder care group chat called Take Five.

Examine the image carefully and do two things:

1. CLASSIFY it as one of:
   - MEDICATION: a prescription bottle, OTC medication, pill bottle, or supplement/vitamin label
   - OTHER: anything else (food, people, places, pets, etc.)

2. Respond with a JSON object in this exact format:

If MEDICATION:
{
  "classification": "MEDICATION",
  "description": "Brief one-sentence description of what you see",
  "extracted": {
    "medication_name": "full name as shown on label",
    "brand_name": "brand name if different from medication name, else null",
    "dosage": "strength/dosage (e.g. 10mg, 500mg)",
    "form": "tablet, capsule, liquid, etc.",
    "instructions": "dosing instructions as written on label",
    "patient_name": "name on label if visible, else null",
    "prescriber": "prescribing doctor if visible, else null",
    "pharmacy": "pharmacy name if visible, else null",
    "refill_date": "refill date if visible, else null",
    "quantity": "quantity if visible, else null",
    "rx_number": "prescription number if visible, else null",
    "is_supplement": true or false
  },
  "confidence": "high | medium | low",
  "notes": "anything else worth flagging (e.g. label partially obscured)"
}

If OTHER:
{
  "classification": "OTHER",
  "description": "Warm one-sentence description suitable for a care log entry",
  "extracted": null,
  "confidence": "high",
  "notes": null
}

Return only valid JSON. No preamble, no markdown code fences.
"""


def get_image_attachment(payload: dict) -> str | None:
    """Extract image URL from GroupMe webhook payload. Returns None if no image."""
    attachments = payload.get("attachments", [])
    for attachment in attachments:
        if attachment.get("type") == "image":
            return attachment.get("url")
    return None


async def fetch_image_as_base64(url: str) -> tuple[str, str]:
    """
    Fetch image from GroupMe URL and return (base64_data, media_type).
    GroupMe URLs require no auth for images already uploaded.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()

    content_type = response.headers.get("content-type", "image/jpeg")
    # Normalize to just the mime type (strip charset etc.)
    media_type = content_type.split(";")[0].strip()
    # Default to jpeg if unclear
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"

    import base64
    image_data = base64.standard_b64encode(response.content).decode("utf-8")
    return image_data, media_type


async def analyze_image(image_url: str, sender_name: str, message_text: str) -> dict:
    """
    Send image to Claude vision for classification and extraction.
    Returns parsed result dict.
    """
    logger.info(f"[images] Fetching image from {image_url}")
    image_data, media_type = await fetch_image_as_base64(image_url)

    logger.info(f"[images] Sending to Claude vision — sender: {sender_name}, text hint: '{message_text}'")

    # Build the user message — include any text the sender wrote as a hint
    user_content = []

    if message_text:
        user_content.append({
            "type": "text",
            "text": f'The sender wrote: "{message_text}"\n\nNow analyze the image:'
        })

    user_content.append({
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": image_data,
        }
    })

    response = ANTHROPIC_CLIENT.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=VISION_PROMPT,
        messages=[{"role": "user", "content": user_content}]
    )

    raw_text = response.content[0].text.strip()
    logger.info(f"[images] Raw vision response: {raw_text}")

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"[images] Failed to parse vision response as JSON: {e}")
        result = {
            "classification": "ERROR",
            "description": "Could not parse vision response",
            "raw": raw_text,
            "extracted": None,
            "confidence": "low",
            "notes": str(e)
        }

    return result


async def handle_image_message(payload: dict) -> dict | None:
    """
    Main entry point called from webhook handler.
    Returns analysis result dict, or None if no image found.
    Logs everything — no DB writes in Phase 1.
    """
    image_url = get_image_attachment(payload)
    if not image_url:
        return None

    sender_name = payload.get("name", "Unknown")
    message_text = payload.get("text", "").strip()
    sender_id = payload.get("sender_id")
    group_id = payload.get("group_id")
    message_id = payload.get("id")

    logger.info(
        f"[images] Image detected — "
        f"sender: {sender_name} ({sender_id}), "
        f"group: {group_id}, "
        f"message_id: {message_id}, "
        f"url: {image_url}"
    )

    try:
        result = await analyze_image(image_url, sender_name, message_text)
    except Exception as e:
        logger.error(f"[images] Vision call failed: {e}", exc_info=True)
        return None

    # Phase 1: Log the full result for inspection
    logger.info(f"[images] Classification: {result.get('classification')}")
    logger.info(f"[images] Description: {result.get('description')}")
    logger.info(f"[images] Confidence: {result.get('confidence')}")

    if result.get("classification") == "MEDICATION":
        extracted = result.get("extracted", {})
        logger.info(f"[images] MEDICATION DETECTED — extracted fields:")
        logger.info(f"[images]   name:         {extracted.get('medication_name')}")
        logger.info(f"[images]   brand:        {extracted.get('brand_name')}")
        logger.info(f"[images]   dosage:       {extracted.get('dosage')}")
        logger.info(f"[images]   form:         {extracted.get('form')}")
        logger.info(f"[images]   instructions: {extracted.get('instructions')}")
        logger.info(f"[images]   patient:      {extracted.get('patient_name')}")
        logger.info(f"[images]   prescriber:   {extracted.get('prescriber')}")
        logger.info(f"[images]   pharmacy:     {extracted.get('pharmacy')}")
        logger.info(f"[images]   is_supplement:{extracted.get('is_supplement')}")
        logger.info(f"[images]   confidence:   {result.get('confidence')}")
        if result.get("notes"):
            logger.info(f"[images]   notes:        {result.get('notes')}")
    else:
        logger.info(f"[images] OTHER image — care log description: {result.get('description')}")

    return result
