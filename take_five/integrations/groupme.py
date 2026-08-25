import asyncio
import logging
import os
import re

import httpx
from typing import Optional

from take_five.repository import repo
from take_five.pipeline import run_post_storage_pipeline, run_memory, run_signal_detection
from take_five.messages import (
    ask_with_tools,
    generate_prep_packet,
    parse_prep_request,
    resolve_prep_seniors,
)
from take_five.images import extract_groupme_image, handle_image_message

logger = logging.getLogger(__name__)

GROUPME_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_IMAGE_SERVICE_URL = "https://image.groupme.com/pictures"
GROUPME_HEADERS = {
    "User-Agent": "curl/7.68.0",
    "Content-Type": "application/json"
}

GROUPME_MAX_CHARS = 4000


async def upload_image_to_groupme(image_bytes: bytes, content_type: str) -> Optional[str]:
    """
    Upload image bytes to GroupMe's Image Service so they can be attached to a bot
    post. Bot posts can only reference i.groupme.com-hosted images via `picture_url`
    — external URLs (e.g. Twilio media URLs) are not accepted directly, so images
    arriving from other channels (SMS, future WhatsApp) must be re-hosted here first.

    Requires GROUPME_USER_ACCESS_TOKEN — the same user access token already used
    for group/bot setup in setup_groupme_circle(). Bots don't have their own token
    for the Image Service.

    Returns the picture_url, or None on failure (caller should fall back to a
    text-only post rather than dropping the message).
    """
    access_token = os.getenv("GROUPME_USER_ACCESS_TOKEN")
    if not access_token:
        logger.error("[groupme] GROUPME_USER_ACCESS_TOKEN not set — cannot upload image")
        return None

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            GROUPME_IMAGE_SERVICE_URL,
            headers={
                "X-Access-Token": access_token,
                "Content-Type": content_type,
            },
            content=image_bytes,
        )

    if response.status_code != 200:
        logger.error(f"[groupme] Image upload failed: {response.status_code} - {response.text}")
        return None

    payload = response.json().get("payload", {})
    picture_url = payload.get("picture_url") or payload.get("url")
    if not picture_url:
        logger.error(f"[groupme] Image upload response missing picture_url: {response.text}")
        return None
    return picture_url


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


async def send_message_async(bot_id: str, text: str, picture_url: Optional[str] = None) -> bool:
    """Async version for use inside the FastAPI webhook.

    bot_id comes from care_circles.integration_config['groupme_bot_id'].
    Automatically splits text that exceeds GROUPME_MAX_CHARS at sentence
    boundaries and sends each chunk sequentially. If picture_url is given
    (an i.groupme.com URL from upload_image_to_groupme), it's attached to
    the first chunk only, so a long message with an image still posts as
    one photo attached to one logical reply, not one per chunk.
    """
    chunks = split_for_groupme(text)
    all_ok = True
    async with httpx.AsyncClient() as client:
        for i, chunk in enumerate(chunks):
            body = {"bot_id": bot_id, "text": chunk}
            if picture_url and i == 0:
                body["picture_url"] = picture_url
            response = await client.post(
                GROUPME_URL,
                json=body,
                headers=GROUPME_HEADERS
            )
            if response.status_code == 202:
                logger.info(f"Message chunk sent successfully to GroupMe ({len(chunk)} chars)")
            else:
                logger.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
                all_ok = False
    return all_ok


async def groupme_reply(bot_id: Optional[str], text: Optional[str], circle_ext_id: Optional[str] = None, picture_url: Optional[str] = None):
    """
    Post a reply to GroupMe and log it as an outbound agent_note.
    No-op if bot_id is missing, or if there's neither text nor an image to send.

    Internal sentinels ([SAVED: ...], [PATCHED: ...]) are stripped from the
    visible GroupMe message but preserved in the logged body so Claude can
    read them in future turns for state tracking.
    """
    if not bot_id or (not text and not picture_url):
        return
    # Strip sentinel lines before posting — users never see them
    visible_text = "\n".join(
        line for line in (text or "").splitlines()
        if not line.startswith("[SAVED:") and not line.startswith("[PATCHED:")
    ).strip()
    await send_message_async(bot_id, visible_text, picture_url=picture_url)
    if circle_ext_id:
        try:
            raw_data = {"source": "t5_bot", "bot_id": bot_id}
            if picture_url:
                raw_data["picture_url"] = picture_url
            repo.log_message(
                circle_ext_id=circle_ext_id,
                person_ext_id=None,
                body=text or "[image]",
                raw_data=raw_data,
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

        # Status guard: archived circles are fully offboarded — no ingestion,
        # no signal detection, no @T5. Belt-and-suspenders alongside bot
        # destruction; makes the admin status toggle a true kill switch.
        if circle and circle.get('status') != 'active':
            logger.info(f"[groupme] Circle '{circle['name']}' is {circle['status']} — dropping message")
            return {"status": "ignored"}

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

        image_attachment = extract_groupme_image(data)

        if not image_attachment:
            asyncio.create_task(run_post_storage_pipeline(
                message_id=str(new_msg['id']),
                circle_id=str(new_msg['circle_id']),
                body=text,
                sender=person_name,
                sent_at=new_msg['sent_at'],
                channel="groupme",
            ))
        # else: nothing fires yet. For an image, memory and signal detection
        # are deferred together until vision classification resolves (and,
        # for DOCUMENT, until caption + OCR text are combined) — see
        # finalize_image_pipeline() below, called exactly once on the final body.

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
        if image_attachment:
            def finalize_image_pipeline(body: str, sender_name: str, sent_at):
                """
                Fire memory (chunk/embed) and signal detection together,
                exactly once, on whatever text ends up being the message's
                final content. Called from every resolution point below
                (vision failure, DOCUMENT success, DOCUMENT with no usable
                text, MEDICATION/OTHER) so an image message always gets both
                passes exactly once — never zero (silently unsearchable),
                never twice (duplicate signal rows, since clinical_signals
                has no dedupe constraint).
                """
                asyncio.create_task(run_memory(
                    message_id=str(new_msg['id']),
                    circle_id=str(new_msg['circle_id']),
                    body=body,
                    sender=sender_name,
                    sent_at=sent_at,
                ))
                asyncio.create_task(run_signal_detection(
                    message_id=str(new_msg['id']),
                    circle_id=str(new_msg['circle_id']),
                    body=body,
                    channel="groupme",
                ))

            async def process_image():
                result = await handle_image_message(image_attachment)
                if not result:
                    # Vision call failed — still process the caption alone
                    # rather than silently dropping it.
                    finalize_image_pipeline(text, person_name, new_msg['sent_at'])
                    return
                reply, vision_result = result
                if reply:
                    await groupme_reply(bot_id, reply, circle_ext_id)

                classification = vision_result.get("classification")

                if classification == "DOCUMENT":
                    extracted_text = (vision_result.get("extracted_text") or "").strip()
                    if not extracted_text:
                        logger.warning(
                            f"[groupme] DOCUMENT classified but no extracted_text — "
                            f"message {new_msg['id']}"
                        )
                        finalize_image_pipeline(text, person_name, new_msg['sent_at'])
                        return
                    caption = image_attachment.message_text
                    enriched_body = (
                        f"{caption}\n\n[Document text]:\n{extracted_text}"
                        if caption else
                        f"[Document text]:\n{extracted_text}"
                    )
                    confidence = vision_result.get("confidence", "medium")
                    updated = repo.update_message_body(
                        message_id=str(new_msg['id']),
                        body=enriched_body,
                        raw_data={"ocr": {"detected": True, "confidence": confidence}},
                    )
                    logger.info(
                        f"[groupme] DOCUMENT — appended OCR text to message "
                        f"{new_msg['id']} ({len(extracted_text)} chars, confidence: {confidence})"
                    )
                    # Now that caption + OCR text are combined, this is the
                    # first and only time memory/signal detection run.
                    finalize_image_pipeline(enriched_body, image_attachment.sender_name, updated['sent_at'])
                    return

                # Build a clean log body for the messages table
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

                # No document text to combine with — process the caption
                # alone, deferred until here so this is still the first and
                # only pass for this message.
                finalize_image_pipeline(text, person_name, new_msg['sent_at'])
            asyncio.create_task(process_image())

        # 4. T5 ask flow — ask_with_tools handles both Q&A and medication saves
        t5_match = re.search(r'@T5', text, re.IGNORECASE)
        if t5_match:
            question = text.strip()
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
                logger.info("[groupme] Prep packet follow-up trigger detected")
                async def run_prep_followup():
                    try:
                        roster = repo.fetch_circle_roster(circle_id)
                        seniors = [r for r in roster if r.get("person_role") == "senior"]

                        packets = repo.get_prep_packets(circle_id, limit=20)
                        if not packets:
                            await groupme_reply(
                                bot_id,
                                "I don't have a prep packet started for this circle yet — "
                                "send @T5 prep for [name]'s appointment with [doctor] to start one.",
                                circle_ext_id,
                            )
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
                        await send_message_async(bot_id, packet_text)
                        await asyncio.sleep(1.5)
                        await groupme_reply(bot_id, followup_text, circle_ext_id)
                    except Exception as e:
                        logger.error(f"[groupme] Prep packet follow-up failed: {e}", exc_info=True)
                        await groupme_reply(
                            bot_id,
                            "Sorry, I ran into a problem updating the prep packet. Try again or ask @T5 directly.",
                            circle_ext_id,
                        )
                asyncio.create_task(run_prep_followup())
            elif is_prep_trigger:
                logger.info("[groupme] Prep packet trigger detected")
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
                        seniors = [r for r in roster if r.get("person_role") == "senior"]

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
                                await groupme_reply(
                                    bot_id,
                                    f"Prep pack for {names}? Send @T5 prep for [name]'s "
                                    f"appointment with the doctor/appointment details and I'll put it together.",
                                    circle_ext_id,
                                )
                                return

                        # Parse doctor/appointment once and reuse it across every
                        # senior's packet (avoids a duplicate Haiku call per senior
                        # for the "mom and dad, same appointment" case).
                        parsed = await parse_prep_request(question)
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
                            await send_message_async(bot_id, packet_text)
                            await asyncio.sleep(1.5)
                            await groupme_reply(bot_id, followup_text, circle_ext_id)
                            if i < len(target_seniors) - 1:
                                await asyncio.sleep(1.5)
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


def resolve_groupme_token(admin_person_id: Optional[str] = None) -> str:
    """
    Resolve which GroupMe access token to use for an action, per the
    OAuth-per-admin design (Trello #39, 2026-08-02).

    If admin_person_id is given and has a stored per-admin token (from the
    OAuth flow — see connect_groupme_account() and
    /app/groupme/oauth/token in main.py), that token is used. This is the
    account that actually created/owns the relevant GroupMe group, so it's
    the only token with standing to act on it (confirmed: GroupMe's
    "Admin only" member-management permission authorizes the group owner
    specifically — there's no API-level way to extend that to a second
    account).

    Falls back to GROUPME_USER_ACCESS_TOKEN when admin_person_id is None or
    has no stored token — this is what keeps Landry/Addams's existing
    circles (created the old way, before this design existed) working
    unchanged, and is the source of truth for circles created before any
    admin had gone through the OAuth flow. See card #63's remaining-scope
    note: the six call sites that use this fallback are migrated
    incrementally, not all at once.

    Raises ValueError if neither a per-admin token nor the env var fallback
    is available.
    """
    if admin_person_id:
        credential = repo.get_person_channel_credential(admin_person_id, "groupme")
        if credential and credential.get("access_token"):
            return credential["access_token"]
    fallback = os.getenv("GROUPME_USER_ACCESS_TOKEN")
    if not fallback:
        raise ValueError(
            f"No GroupMe token available — no stored credential for "
            f"admin_person_id={admin_person_id} and GROUPME_USER_ACCESS_TOKEN not set"
        )
    return fallback


async def get_groupme_user_id(access_token: str) -> str:
    """
    Resolve the GroupMe account identity behind an access token, via
    GET /users/me. Called once right after an admin completes the OAuth
    popup flow, so the token and the resulting person_channel_identities /
    person_channel_credentials rows get written together — see
    /app/groupme/oauth/token in main.py.
    """
    GROUPME_API_BASE = "https://api.groupme.com/v3"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GROUPME_API_BASE}/users/me",
            params={"token": access_token},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"GroupMe /users/me failed: {resp.status_code} {resp.text}")
    return str(resp.json()["response"]["id"])


async def lock_group_member_management(group_id: str, access_token: str) -> bool:
    """
    Sets a GroupMe group's group_type to 'closed', which restricts member-
    roster and settings management to the group owner (the admin whose
    token created it — see resolve_groupme_token) while leaving messaging
    open to everyone. This makes the Take Five admin app the sole path for
    adding new chat members going forward — a plain GroupMe-native "add to
    group" from any other member is blocked.

    Deliberately 'closed', not 'announcement': 'announcement' also restricts
    who can *send messages*, which would break normal family/caregiver
    check-ins. 'closed' only restricts roster/settings management.

    Per GroupMe's community API docs (the official dev API reference doesn't
    document this endpoint at all — POST /groups/:id/update is undocumented
    on the official page but confirmed working via community docs, checked
    2026-08-01).

    access_token: the same token used to create the group (see
    setup_groupme_circle) — passed in explicitly rather than re-resolved
    here, since the caller already has it and this avoids a second lookup.

    Returns True on success. Failure is logged but non-fatal — a family's
    circle is still usable without the lock, just without the member-add
    protection (see Trello #59/#39). Caller (setup_groupme_circle) does not
    fail circle creation over this.
    """
    GROUPME_API_BASE = "https://api.groupme.com/v3"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GROUPME_API_BASE}/groups/{group_id}/update",
            params={"token": access_token},
            json={"group_type": "closed"},
        )
    if resp.status_code != 200:
        logger.warning(
            f"[groupme-setup] Failed to lock member management for group {group_id}: "
            f"{resp.status_code} {resp.text}"
        )
        return False
    logger.info(f"[groupme-setup] Locked member management (group_type=closed) for group {group_id}")
    return True


async def add_person_to_groupme(circle_id: str, person_id: str) -> dict:
    """
    Add a single existing person (already a circle_membership) to the
    circle's GroupMe group, and record the result on circle_memberships
    (chat_membership_id, chat_added_at).

    Prefers adding by GroupMe user_id when the person already has one on
    record (people.external_id, format 'groupme:{user_id}' — set either by
    the webhook's first-message name-match fallback, or by this same
    function's own backfill below on a previous add elsewhere). This is the
    "easy" case: no phone number needed, no re-invite flow, and it adds the
    exact known account directly rather than going through a phone-based
    invite that could in principle land on a different/new GroupMe account
    if the phone number changed or was reassigned. Falls back to phone_number
    (the original behavior) only when no known user_id exists yet — i.e. the
    person has never been resolved to a GroupMe identity in any circle.

    Explicit, per-person action — not automatic on circle_membership
    creation. See migration 009_chat_membership.sql and Trello #59.

    Raises ValueError for caller-fixable problems (no bot/group configured,
    no known user_id AND no phone number, person not a circle member).
    Raises RuntimeError for unexpected GroupMe API failures.
    """
    GROUPME_API_BASE = "https://api.groupme.com/v3"
    GROUPME_ACCESS_TOKEN = os.getenv("GROUPME_USER_ACCESS_TOKEN")
    if not GROUPME_ACCESS_TOKEN:
        raise ValueError("GROUPME_USER_ACCESS_TOKEN not set in environment")

    circle = repo.get_circle_by_id(circle_id)
    if not circle:
        raise ValueError(f"Circle {circle_id} not found")
    group_id = (circle.get('integration_config') or {}).get('groupme_group_id')
    if not group_id:
        raise ValueError(f"Circle {circle_id} has no GroupMe group configured — run setup first")

    person = repo.get_person_by_id(person_id)
    if not person:
        raise ValueError(f"Person {person_id} not found")

    membership = repo.get_circle_membership(circle_id, person_id)
    if not membership:
        raise ValueError(f"'{person['name']}' is not a member of this circle — add them to the circle first")

    # Prefer a known GroupMe user_id over phone-based invite — see docstring.
    known_external_id = person.get('external_id') or ''
    known_user_id = known_external_id.split(':', 1)[1] if known_external_id.startswith('groupme:') else None

    if known_user_id:
        member_payload = {"nickname": person['name'], "user_id": known_user_id}
        logger.info(f"[groupme] Adding {person['name']} to group {group_id} by known user_id")
    else:
        if not person.get('phone'):
            raise ValueError(
                f"'{person['name']}' has no known GroupMe identity and no phone number on "
                f"record — need one or the other to add them to chat"
            )
        # Normalize E.164 (+15127404620) to GroupMe's expected format (+1 5127404620)
        phone = person['phone']
        if phone.startswith('+1') and len(phone) == 12:
            phone = f"+1 {phone[2:]}"
        member_payload = {"nickname": person['name'], "phone_number": phone}
        logger.info(f"[groupme] Adding {person['name']} to group {group_id} by phone (no known user_id yet)")

    async with httpx.AsyncClient() as client:
        add_resp = await client.post(
            f"{GROUPME_API_BASE}/groups/{group_id}/members/add",
            params={"token": GROUPME_ACCESS_TOKEN},
            json={"members": [member_payload]},
        )
        if add_resp.status_code != 202:
            raise RuntimeError(f"GroupMe member add failed: {add_resp.status_code} {add_resp.text}")
        results_id = add_resp.json()['response']['results_id']

        # members/add is async — poll members/results for the real
        # membership id. Results are only available for 1 hour and can
        # 503 briefly while GroupMe processes the add, so retry a few times
        # rather than failing on the first miss.
        membership_id = None
        user_id = None
        for attempt in range(5):
            await asyncio.sleep(1.5)
            results_resp = await client.get(
                f"{GROUPME_API_BASE}/groups/{group_id}/members/results/{results_id}",
                params={"token": GROUPME_ACCESS_TOKEN},
            )
            if results_resp.status_code == 200:
                members = results_resp.json().get('response', {}).get('members', [])
                if members:
                    membership_id = members[0].get('id')
                    user_id = members[0].get('user_id')
                break
            elif results_resp.status_code == 503:
                continue
            else:
                logger.warning(
                    f"[groupme] members/results returned {results_resp.status_code} "
                    f"for {person['name']}, giving up polling"
                )
                break

    if not membership_id:
        # The add call itself succeeded (202) even if we couldn't confirm
        # the resulting membership_id — GroupMe will still deliver the
        # invite. Log clearly rather than raising, so the caller isn't told
        # the whole operation failed when it likely didn't.
        logger.warning(
            f"[groupme] Added {person['name']} to group {group_id} but could not "
            f"confirm membership_id via results polling — recording add without it"
        )

    # members/results also returns the person's GroupMe user_id — the same
    # identifier the webhook matches incoming messages against
    # (person_ext_id = f"groupme:{sender_id}" in handle_groupme_webhook).
    # Writing it here means this person's external_id is set deterministically
    # from the API response, not left to the webhook's name-matching
    # fallback the first time they post — the exact fragility card #59 was
    # opened over. Only set if not already present, so this never clobbers
    # an existing (possibly differently-sourced) external_id; a unique-
    # constraint failure here is logged but non-fatal, since chat_membership_id
    # is already recorded and more important to preserve than this backfill.
    if user_id and not person.get('external_id'):
        try:
            repo.update_person(person_id, external_id=f"groupme:{user_id}")
            logger.info(f"[groupme] Backfilled external_id for {person['name']} from members/results")
        except Exception as e:
            logger.warning(f"[groupme] Could not backfill external_id for {person['name']}: {e}")

    updated = repo.record_chat_membership(circle_id, person_id, chat_membership_id=membership_id)
    logger.info(f"[groupme] Added {person['name']} to GroupMe group {group_id} (membership_id={membership_id})")

    # Fresh phone-based invites hit GroupMe's 12-message SMS trial limit —
    # per GroupMe's own support docs (#stay command, checked 2026-08-01),
    # someone added by phone/SMS only receives their first 12 messages in a
    # group unless they text back "#stay". Someone added by an already-known
    # user_id has an established GroupMe presence and isn't subject to this
    # fresh-invite limit, so the nudge is unnecessary (and would be
    # confusing) for that path. Without this, a new aide or family member
    # who never installs the app would silently stop receiving updates
    # after their 12th message with no signal to anyone that it happened —
    # a direct risk to the "zero behavior change" thesis. See Trello
    # verification card (2026-08-01) for confirming this with a real device.
    if not known_user_id:
        bot_id = (circle.get('integration_config') or {}).get('groupme_bot_id')
        circle_ext_id = circle.get('external_id')
        if bot_id:
            await groupme_reply(
                bot_id,
                f"Hi {person['name']} — welcome! If you're using GroupMe by text only "
                f"(no app), reply #stay to this group so you keep getting updates — "
                f"GroupMe limits new text-only members to their first 12 messages "
                f"otherwise. If you have the app, you can ignore this.",
                circle_ext_id,
            )

    return {
        'person_id': person_id,
        'person_name': person['name'],
        'group_id': group_id,
        'chat_membership_id': membership_id,
        'chat_added_at': str(updated.get('chat_added_at')) if updated else None,
    }


async def remove_person_from_groupme(circle_id: str, person_id: str) -> dict:
    """
    Remove a single person from a circle's GroupMe group, and clear
    chat_membership_id/chat_added_at on their circle_membership (via
    repo.clear_chat_membership) so the roster and add button correctly
    reflect that they're no longer in the chat. Does NOT remove them from
    the circle itself (circle_memberships row stays — that's a separate
    action, remove_person_from_circle). Does NOT touch people.external_id
    — their GroupMe identity remains valid for other circles.

    Uses circle_memberships.chat_membership_id when available (the fast,
    already-correct path). If it's missing — e.g. a person added before
    migration 009_chat_membership.sql existed, or before the backfill script
    ran — falls back to a live lookup via GET /groups/:group_id, matching
    on the person's known user_id (people.external_id). This makes the
    function self-healing rather than requiring the backfill to have run
    first.

    Per GroupMe's API: the group creator cannot be removed (will surface as
    a RuntimeError from a non-200 response) — see Trello #39, confirmed
    2026-08-01. Explicit, per-person action, mirroring add_person_to_groupme.
    See migration 009_chat_membership.sql and Trello #59.

    Raises ValueError for caller-fixable problems (no group configured,
    person not a circle member, person not currently in the chat, or their
    membership id couldn't be resolved even via fallback). Raises
    RuntimeError for unexpected GroupMe API failures (including "can't
    remove the group creator").
    """
    GROUPME_API_BASE = "https://api.groupme.com/v3"
    GROUPME_ACCESS_TOKEN = os.getenv("GROUPME_USER_ACCESS_TOKEN")
    if not GROUPME_ACCESS_TOKEN:
        raise ValueError("GROUPME_USER_ACCESS_TOKEN not set in environment")

    circle = repo.get_circle_by_id(circle_id)
    if not circle:
        raise ValueError(f"Circle {circle_id} not found")
    group_id = (circle.get('integration_config') or {}).get('groupme_group_id')
    if not group_id:
        raise ValueError(f"Circle {circle_id} has no GroupMe group configured")

    person = repo.get_person_by_id(person_id)
    if not person:
        raise ValueError(f"Person {person_id} not found")

    membership = repo.get_circle_membership(circle_id, person_id)
    if not membership:
        raise ValueError(f"'{person['name']}' is not a member of this circle")

    chat_membership_id = membership.get('chat_membership_id')

    async with httpx.AsyncClient() as client:
        if not chat_membership_id:
            # Self-healing fallback — look up the live member list and match
            # by known user_id, same approach as backfill_chat_membership.py.
            known_external_id = person.get('external_id') or ''
            known_user_id = known_external_id.split(':', 1)[1] if known_external_id.startswith('groupme:') else None
            if not known_user_id:
                raise ValueError(
                    f"'{person['name']}' has no recorded chat_membership_id and no known "
                    f"GroupMe identity to look one up with — can't determine if or how "
                    f"they're in this group"
                )
            group_resp = await client.get(
                f"{GROUPME_API_BASE}/groups/{group_id}",
                params={"token": GROUPME_ACCESS_TOKEN},
            )
            if group_resp.status_code != 200:
                raise RuntimeError(f"GroupMe group fetch failed: {group_resp.status_code} {group_resp.text}")
            live_members = group_resp.json().get('response', {}).get('members', [])
            match = next((m for m in live_members if m.get('user_id') == known_user_id), None)
            if not match:
                # Not actually in the group per GroupMe itself — just clear
                # our stale local state rather than erroring, since the end
                # state the caller wants (not in chat) is already true.
                repo.clear_chat_membership(circle_id, person_id)
                logger.info(
                    f"[groupme] {person['name']} wasn't actually in group {group_id} "
                    f"(stale local state cleared)"
                )
                return {'person_id': person_id, 'person_name': person['name'], 'group_id': group_id, 'already_absent': True}
            chat_membership_id = match['id']
            logger.info(f"[groupme] Resolved missing chat_membership_id for {person['name']} via live lookup")

        remove_resp = await client.post(
            f"{GROUPME_API_BASE}/groups/{group_id}/members/{chat_membership_id}/remove",
            params={"token": GROUPME_ACCESS_TOKEN},
        )
        if remove_resp.status_code != 200:
            raise RuntimeError(
                f"GroupMe member remove failed: {remove_resp.status_code} {remove_resp.text} "
                f"(note: the group creator cannot be removed)"
            )

    repo.clear_chat_membership(circle_id, person_id)
    logger.info(f"[groupme] Removed {person['name']} from GroupMe group {group_id}")
    return {'person_id': person_id, 'person_name': person['name'], 'group_id': group_id, 'already_absent': False}


async def setup_groupme_circle(circle_id: str, admin_person_id: Optional[str] = None) -> dict:
    """
    Programmatically sets up a GroupMe group and bot for a care circle.
    Creates an empty group — nobody is added here, not even the ensemble
    admin. Adding people (any role, including the admin) always goes through
    add_person_to_groupme(), the same explicit per-person action, once they
    already hold a circle_membership. See Trello #59, 2026-08-01: the
    intended flow is (1) create circle, (2) add ensemble members to the
    circle, (3) create the GroupMe group via this function, (4) per-person
    "add to GroupMe" for whoever should be in the chat — admin included, no
    special-cased auto-invite.

    admin_person_id: the person whose GroupMe account should create (and
    therefore own) this circle's group — see resolve_groupme_token() and
    Trello #39's OAuth-per-admin design. None falls back to
    GROUPME_USER_ACCESS_TOKEN, i.e. the pre-#39 behavior — kept as the
    default so existing callers (the secure_router endpoint, superadmin
    tooling) keep working unchanged until they're deliberately switched
    over to passing a real admin_person_id.

    Steps:
      1. Fetch the circle
      2. Resolve which token/account creates the group (admin_person_id's
         stored OAuth token, or GROUPME_USER_ACCESS_TOKEN)
      3. Create the GroupMe group
      4. Register the Take Five bot in the group
      5. Lock member management to admin-only (see lock_group_member_management)
      6. Store group_id, bot_id, external_id, and (if admin_person_id was
         given) groupme_admin_person_id back on the circle record — that
         last field is what add_person_to_groupme()/remove_person_from_groupme()
         will look up later to resolve the same admin's token for ongoing
         member management (not yet wired — see card #63 remaining scope).

    Returns a summary dict with group_id and bot_id.
    """
    GROUPME_API_BASE = "https://api.groupme.com/v3"
    GROUPME_CALLBACK_URL = "https://app.takefive.care/groupme/webhook"
    BOT_NAME = "Take Five"

    access_token = resolve_groupme_token(admin_person_id)

    # 1. Fetch the circle
    circle = repo.get_circle_by_id(circle_id)
    if not circle:
        raise ValueError(f"Circle {circle_id} not found")
    circle_name = circle['name']

    async with httpx.AsyncClient() as client:
        # 2. Create the GroupMe group — the admin's account (or the legacy
        # env-var account) becomes the owner automatically, no transfer step.
        group_resp = await client.post(
            f"{GROUPME_API_BASE}/groups",
            params={"token": access_token},
            json={"name": circle_name},
        )
        if group_resp.status_code != 201:
            raise RuntimeError(f"GroupMe group creation failed: {group_resp.status_code} {group_resp.text}")
        group_id = group_resp.json()['response']['id']
        logger.info(f"[groupme-setup] Created group '{circle_name}' with id {group_id}"
                    f"{f' (owner: person {admin_person_id})' if admin_person_id else ' (owner: env var account)'}")

        # 3. Register the bot
        bot_resp = await client.post(
            f"{GROUPME_API_BASE}/bots",
            params={"token": access_token},
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

        # 4. Lock member management to admin-only — see
        # lock_group_member_management(). Non-fatal on failure; a warning
        # is already logged inside the function, so circle setup proceeds
        # either way rather than leaving the family without a group at all.
        await lock_group_member_management(group_id, access_token)

    # 5. Store group_id, bot_id, external_id, and (if known) which admin's
    # account owns this group on the circle record.
    integration_config = {
        'groupme_group_id': group_id,
        'groupme_bot_id': bot_id,
    }
    if admin_person_id:
        integration_config['groupme_admin_person_id'] = admin_person_id
    repo.update_care_circle(circle_id, {
        'external_id': f"groupme:{group_id}",
        'integration_config': integration_config,
    })
    logger.info(f"[groupme-setup] Circle {circle_id} updated with groupme config")

    return {
        'group_id': group_id,
        'bot_id': bot_id,
        'group_name': circle_name,
    }

