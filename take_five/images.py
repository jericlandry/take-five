"""
take_five/images.py

Image detection and analysis pipeline — channel-agnostic.

Channel adapters (GroupMe, WhatsApp, SMS) are responsible for extracting
an ImageAttachment from their respective webhook payloads and passing it
to handle_image_message(). This module knows nothing about GroupMe specifics.

Phase 1: Vision classification and extraction — logs to console only.
Phase 2 (future): DB write via tool use after confirmation flow.
"""

import logging
import httpx
import anthropic
import json
import base64
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ANTHROPIC_CLIENT = anthropic.Anthropic()

# Opus for vision — prescription labels are small, curved, often glare-y.
# At family chat volume the cost difference is negligible.
VISION_MODEL = "claude-opus-4-20250514"

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
  "notes": "anything else worth flagging (e.g. label partially obscured, text curved and hard to read)"
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


@dataclass
class ImageAttachment:
    """
    Normalized image event produced by each channel adapter.
    
    Channel webhooks are responsible for constructing this from their
    own payload format before calling handle_image_message().
    
    GroupMe:   extracted in groupme.py from attachments[].type == "image"
    WhatsApp:  extracted from Media URL in WhatsApp webhook payload
    SMS/MMS:   extracted from Twilio NumMedia / MediaUrl0..N fields
    """
    url: str
    sender_name: str
    message_text: str
    sender_id: str
    group_id: str        # circle/group identifier — group_id, phone number, etc.
    message_id: str
    channel: str         # "groupme" | "whatsapp" | "sms"


# ---------------------------------------------------------------------------
# Channel adapters — one per integration
# Each returns an ImageAttachment or None if no image in the payload.
# ---------------------------------------------------------------------------

def extract_groupme_image(payload: dict) -> ImageAttachment | None:
    """Extract image from a GroupMe webhook payload."""
    attachments = payload.get("attachments", [])
    image_url = next(
        (a.get("url") for a in attachments if a.get("type") == "image"),
        None
    )
    if not image_url:
        return None

    return ImageAttachment(
        url=image_url,
        sender_name=payload.get("name", "Unknown"),
        message_text=payload.get("text", "").strip(),
        sender_id=str(payload.get("sender_id", "")),
        group_id=str(payload.get("group_id", "")),
        message_id=str(payload.get("id", "")),
        channel="groupme",
    )


def extract_whatsapp_image(payload: dict) -> ImageAttachment | None:
    """
    Extract image from a WhatsApp Cloud API webhook payload.
    
    WhatsApp delivers media as a media_id that must be fetched separately
    via the Graph API. The URL here should be pre-resolved by the webhook
    handler before calling this function, or handled lazily in fetch_image_as_base64.
    
    Placeholder — implement when WhatsApp integration is added.
    """
    # WhatsApp payload structure (Cloud API):
    # payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["id"]
    # Requires a separate GET to https://graph.facebook.com/{media_id}
    # to resolve to a downloadable URL with Authorization: Bearer {token}
    raise NotImplementedError("WhatsApp image extraction not yet implemented")


def extract_sms_image(payload: dict) -> ImageAttachment | None:
    """
    Extract image from a Twilio MMS webhook payload (form-encoded).
    
    Twilio sends NumMedia, MediaUrl0..N, MediaContentType0..N fields.
    The webhook handler receives these as Form() parameters and should
    pass them here as a normalized dict.
    
    Placeholder — implement when MMS support is added.
    """
    # Twilio MMS fields:
    # NumMedia: number of media attachments
    # MediaUrl0, MediaUrl1, ...: public URLs (no auth required)
    # MediaContentType0, ...: mime types
    num_media = int(payload.get("NumMedia", 0))
    if num_media == 0:
        return None

    # Take the first image attachment
    for i in range(num_media):
        content_type = payload.get(f"MediaContentType{i}", "")
        if content_type.startswith("image/"):
            return ImageAttachment(
                url=payload.get(f"MediaUrl{i}", ""),
                sender_name=payload.get("sender_name", "Unknown"),
                message_text=payload.get("Body", "").strip(),
                sender_id=payload.get("From", ""),
                group_id=payload.get("To", ""),
                message_id=payload.get("MessageSid", ""),
                channel="sms",
            )
    return None


# ---------------------------------------------------------------------------
# Core pipeline — channel-agnostic from here down
# ---------------------------------------------------------------------------

async def fetch_image_as_base64(url: str, headers: dict = None) -> tuple[str, str]:
    """
    Fetch image from URL and return (base64_data, media_type).

    GroupMe: redirects m.groupme.com → cdn2.groupme.com, follow_redirects required.
    Twilio:  MediaUrls are public, no auth needed.
    WhatsApp: requires Authorization: Bearer {token} in headers — pass via headers param.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers or {})
        response.raise_for_status()

    content_type = response.headers.get("content-type", "image/jpeg")
    media_type = content_type.split(";")[0].strip()
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"

    image_data = base64.standard_b64encode(response.content).decode("utf-8")
    return image_data, media_type


async def analyze_image(attachment: ImageAttachment) -> dict:
    """
    Send image to Claude vision for classification and extraction.
    Accepts a normalized ImageAttachment — no channel-specific logic here.
    Returns parsed result dict.
    """
    logger.info(f"[images] Fetching image from {attachment.url}")
    image_data, media_type = await fetch_image_as_base64(attachment.url)

    logger.info(
        f"[images] Sending to Claude vision ({VISION_MODEL}) — "
        f"channel: {attachment.channel}, "
        f"sender: {attachment.sender_name}, "
        f"text hint: '{attachment.message_text}'"
    )

    user_content = []

    if attachment.message_text:
        user_content.append({
            "type": "text",
            "text": f'The sender wrote: "{attachment.message_text}"\n\nNow analyze the image:'
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
        model=VISION_MODEL,
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


async def handle_image_message(attachment: ImageAttachment) -> dict | None:
    """
    Main entry point — accepts a normalized ImageAttachment.
    Called from any channel's webhook handler after extracting the attachment.
    Logs everything — no DB writes in Phase 1.
    """
    logger.info(
        f"[images] Image detected — "
        f"channel: {attachment.channel}, "
        f"sender: {attachment.sender_name} ({attachment.sender_id}), "
        f"group: {attachment.group_id}, "
        f"message_id: {attachment.message_id}, "
        f"url: {attachment.url}"
    )

    try:
        result = await analyze_image(attachment)
    except Exception as e:
        logger.error(f"[images] Vision call failed: {e}", exc_info=True)
        return None

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
