"""
take_five/images.py

Image detection and analysis pipeline — channel-agnostic.

Channel adapters (GroupMe, WhatsApp, SMS) are responsible for extracting
an ImageAttachment from their respective webhook payloads and passing it
to handle_image_message(). This module knows nothing about GroupMe specifics.

Phase 1: Vision classification and extraction — returns reply text to caller.
Phase 2 (future): Confirmation flow and DB write via tool use.
"""

import logging
import httpx
import anthropic
import json
import base64
from dataclasses import dataclass
from typing import Optional

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

    GroupMe:  extracted from attachments[].type == "image"
    WhatsApp: extracted from Media URL in WhatsApp webhook payload
    SMS/MMS:  extracted from Twilio NumMedia / MediaUrl0..N fields
    """
    url: str
    sender_name: str
    message_text: str
    sender_id: str
    group_id: str
    message_id: str
    channel: str        # "groupme" | "whatsapp" | "sms"


# ---------------------------------------------------------------------------
# Channel adapters
# ---------------------------------------------------------------------------

def extract_groupme_image(payload: dict) -> Optional[ImageAttachment]:
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


def extract_whatsapp_image(payload: dict) -> Optional[ImageAttachment]:
    """Placeholder — implement when WhatsApp integration is added."""
    raise NotImplementedError("WhatsApp image extraction not yet implemented")


def extract_sms_image(payload: dict) -> Optional[ImageAttachment]:
    """Extract image from a Twilio MMS webhook payload."""
    num_media = int(payload.get("NumMedia", 0))
    if num_media == 0:
        return None

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
# Formatting
# ---------------------------------------------------------------------------

def format_medication_message(extracted: dict, sender_name: str, confidence: str, notes: str) -> str:
    """Format extracted medication data into a chat-friendly message."""
    lines = [f"💊 Medication label read from {sender_name}'s photo:\n"]

    name = extracted.get("medication_name")
    brand = extracted.get("brand_name")
    if name and brand:
        lines.append(f"Medication: {name} ({brand})")
    elif name:
        lines.append(f"Medication: {name}")

    if extracted.get("dosage"):
        lines.append(f"Dosage: {extracted['dosage']}")
    if extracted.get("form"):
        lines.append(f"Form: {extracted['form']}")
    if extracted.get("instructions"):
        lines.append(f"Instructions: {extracted['instructions']}")
    if extracted.get("patient_name"):
        lines.append(f"Patient: {extracted['patient_name']}")
    if extracted.get("prescriber"):
        lines.append(f"Prescriber: {extracted['prescriber']}")
    if extracted.get("pharmacy"):
        lines.append(f"Pharmacy: {extracted['pharmacy']}")
    if extracted.get("refill_date"):
        lines.append(f"Refill date: {extracted['refill_date']}")
    if extracted.get("quantity"):
        lines.append(f"Quantity: {extracted['quantity']}")
    if extracted.get("is_supplement"):
        lines.append(f"Type: Supplement")

    if confidence != "high":
        lines.append(f"\n⚠️ Confidence: {confidence}")
    if notes:
        lines.append(f"Note: {notes}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def fetch_image_as_base64(url: str, headers: dict = None) -> tuple[str, str]:
    """
    Fetch image from URL and return (base64_data, media_type).
    GroupMe redirects m.groupme.com → cdn2.groupme.com, follow_redirects required.
    WhatsApp requires Authorization: Bearer {token} — pass via headers param.
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
    """Send image to Claude vision. Returns parsed result dict."""
    logger.info(f"[images] Fetching image from {attachment.url}")
    image_data, media_type = await fetch_image_as_base64(attachment.url)

    logger.info(
        f"[images] Sending to Claude vision ({VISION_MODEL}) — "
        f"channel: {attachment.channel}, sender: {attachment.sender_name}"
    )

    user_content = []
    if attachment.message_text:
        user_content.append({
            "type": "text",
            "text": f'The sender wrote: "{attachment.message_text}"\n\nNow analyze the image:'
        })
    user_content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_data}
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
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"[images] Failed to parse vision response as JSON: {e}")
        return {
            "classification": "ERROR",
            "description": "Could not parse vision response",
            "raw": raw_text,
            "extracted": None,
            "confidence": "low",
            "notes": str(e)
        }


async def handle_image_message(attachment: ImageAttachment) -> Optional[str]:
    """
    Main entry point. Returns a reply string if the image warrants one
    (MEDICATION), or None if not. Caller is responsible for posting the reply.
    """
    logger.info(
        f"[images] Image detected — channel: {attachment.channel}, "
        f"sender: {attachment.sender_name} ({attachment.sender_id}), "
        f"group: {attachment.group_id}, message_id: {attachment.message_id}"
    )

    try:
        result = await analyze_image(attachment)
    except Exception as e:
        logger.error(f"[images] Vision call failed: {e}", exc_info=True)
        return None

    classification = result.get("classification")
    logger.info(f"[images] Classification: {classification} | Confidence: {result.get('confidence')}")

    if classification == "MEDICATION":
        extracted = result.get("extracted", {})
        logger.info(f"[images] MEDICATION DETECTED — "
                    f"name: {extracted.get('medication_name')}, "
                    f"dosage: {extracted.get('dosage')}, "
                    f"patient: {extracted.get('patient_name')}")

        return format_medication_message(
            extracted=extracted,
            sender_name=attachment.sender_name,
            confidence=result.get("confidence", "high"),
            notes=result.get("notes") or "",
        )

    logger.info(f"[images] OTHER image — {result.get('description')}")
    return None
