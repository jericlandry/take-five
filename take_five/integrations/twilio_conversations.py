"""
Twilio Conversations webhook: native group MMS on the Take Five Care LLC
number (+1 830-327-0247, messaging service "Take Five Group Texting").

How it fits together (design discussion, Nov 2026):
  - A family member starts a group text that includes the Take Five number.
    Twilio autocreates a Conversation and sends onConversationAdded, with
    MessagingBinding.AuthorAddress = the group's creator and
    MessagingBinding.Address = everyone else in the group.
  - Every message anyone sends in that group reaches the Take Five number
    too, and arrives here as onMessageAdded (Author = sender's phone).
  - One circle maps to one Conversation, recorded in
    care_circles.integration_config['sms_group']:
        {conversation_sid, created_by, participants, status, linked_at}
    status is 'pending_consent' until every participant has opted in
    (consent flow: sms-group-consent.html, built separately), then 'active'.

This module only RECEIVES. Nothing here sends a message, so it is safe to
run before the consent flow exists. While a group is unlinked or still
pending consent, message bodies are NOT stored: we only log that an event
arrived. Content is stored and run through the normal pipeline (memory,
signal detection) once the group is 'active'.

Event handling is a small registry (_HANDLERS) so the consent flow, @T5
replies, and outbound posting can each plug in without touching the route.
"""
import asyncio
import logging
import os

import httpx
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from take_five.integrations.groupme import split_for_groupme
from take_five.pipeline import run_post_storage_pipeline
from take_five.repository import repo
from take_five.t5_commands import handle_t5, is_t5_command

logger = logging.getLogger(__name__)

CHANNEL = "sms_group"
STATUS_PENDING = "pending_consent"
STATUS_ACTIVE = "active"

# One group MMS can carry more than one SMS segment, but every segment is
# billed per recipient; keep chunks well under Twilio's 1,600-char body cap.
SMS_GROUP_MAX_CHARS = 1500

_twilio_client: Optional[TwilioClient] = None


def _client() -> TwilioClient:
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    return _twilio_client


async def _upload_media(conversation_sid: str, image: tuple) -> Optional[str]:
    """Upload image bytes to the conversation's Media Content Service and
    return a media SID (ME...) to attach to a message. Conversations media
    lives per Chat Service, so look the service up from the conversation."""
    data, content_type = image
    try:
        conv = await asyncio.to_thread(_client().conversations.v1.conversations(conversation_sid).fetch)
        url = f"https://mcs.us1.twilio.com/v1/Services/{conv.chat_service_sid}/Media"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url, content=data, headers={"Content-Type": content_type},
                auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")),
            )
        if r.status_code not in (200, 201):
            logger.error(f"[sms-group] Media upload failed: {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("sid")
    except Exception as e:
        logger.error(f"[sms-group] Media upload failed: {e}")
        return None


async def send_to_conversation(conversation_sid: str, author: str, text: str,
                               image: Optional[tuple] = None) -> bool:
    """Post into a circle's native group text as the Take Five number (for an
    autocreated group MMS the projected address is the only valid author).
    Text is cleaned for SMS and split into chunks; an image, if any, rides on
    the first chunk. Our own posts come back through onMessageAdded and are
    skipped there."""
    from take_five.integrations.chat import clean_for_sms_group  # avoid import cycle
    visible = clean_for_sms_group(text)
    media_sid = await _upload_media(conversation_sid, image) if image else None
    if not visible and not media_sid:
        return False
    chunks = split_for_groupme(visible, limit=SMS_GROUP_MAX_CHARS) if visible else [""]
    for i, chunk in enumerate(chunks):
        kwargs = {"author": author}
        if chunk:
            kwargs["body"] = chunk
        if media_sid and i == 0:
            kwargs["media_sid"] = media_sid
        await asyncio.to_thread(
            _client().conversations.v1.conversations(conversation_sid).messages.create, **kwargs
        )
    return True


# ─── Event model ───

@dataclass
class ConversationEvent:
    event_type: str
    conversation_sid: Optional[str]
    message_sid: Optional[str] = None
    author: Optional[str] = None            # onMessageAdded: sender's phone (E.164)
    body: str = ""
    has_media: bool = False
    author_address: Optional[str] = None    # onConversationAdded: group creator
    projected_address: Optional[str] = None  # the Take Five number
    addresses: list = field(default_factory=list)  # other group members
    raw: dict = field(default_factory=dict)


def _parse_addresses(form) -> list:
    """MessagingBinding.Address is a single value for 1:1 conversations and
    a list for autocreated group MMS. Twilio may send repeated keys or one
    comma-separated value; accept both."""
    values = form.getlist("MessagingBinding.Address")
    out = []
    for v in values:
        for part in str(v).replace("[", "").replace("]", "").replace('"', "").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def parse_event(form) -> ConversationEvent:
    raw = {k: form.get(k) for k in form.keys()}
    media = form.get("Media")
    return ConversationEvent(
        event_type=form.get("EventType", ""),
        conversation_sid=form.get("ConversationSid"),
        message_sid=form.get("MessageSid"),
        author=form.get("Author"),
        body=form.get("Body") or "",
        has_media=bool(media and media not in ("[]", "null")),
        author_address=form.get("MessagingBinding.AuthorAddress"),
        projected_address=form.get("MessagingBinding.ProjectedAddress"),
        addresses=_parse_addresses(form),
        raw=raw,
    )


# ─── Signature validation ───

def _public_url(request: Request) -> str:
    """The URL Twilio signed. Render terminates TLS at its proxy, so rebuild
    the scheme/host from the forwarded headers rather than request.url."""
    base = os.getenv("PUBLIC_BASE_URL")
    if base:
        url = base.rstrip("/") + request.url.path
    else:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    return url


def validate_signature(request: Request, form) -> None:
    if os.getenv("TWILIO_VALIDATE_SIGNATURE", "true").lower() == "false":
        return
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        logger.error("[sms-group] TWILIO_AUTH_TOKEN not set; rejecting webhook")
        raise HTTPException(status_code=500, detail="Twilio not configured")
    validator = RequestValidator(token)
    signature = request.headers.get("X-Twilio-Signature", "")
    params = {k: form.get(k) for k in form.keys()}
    if not validator.validate(_public_url(request), params, signature):
        logger.warning(f"[sms-group] Invalid Twilio signature for {_public_url(request)}")
        raise HTTPException(status_code=403, detail="Invalid signature")


# ─── Handlers ───

async def on_conversation_added(event: ConversationEvent) -> None:
    """A new group text that includes the Take Five number. Link it to a
    circle only in the unambiguous case: the creator is an SMS-active member
    of exactly one circle that has no group yet. Everything else is logged
    for now; the consent flow decides what to do with unknown creators."""
    creator = event.author_address
    # Logged in full so we can see exactly what Twilio sends for group MMS
    # autocreates (e.g. it may omit ProjectedAddress and include the Take
    # Five number in the Address list instead).
    logger.info(f"[sms-group] onConversationAdded fields: {event.raw}")
    logger.info(
        f"[sms-group] Conversation {event.conversation_sid} created by {creator} "
        f"with {len(event.addresses)} other participant(s)"
    )
    if not creator:
        logger.info(f"[sms-group] {event.conversation_sid} has no AuthorAddress (not a group MMS autocreate); ignoring")
        return

    existing = repo.find_circle_by_sms_group_conversation(event.conversation_sid)
    if existing:
        return  # duplicate delivery (Twilio retries)

    matches = repo.find_active_sms_members_by_phone(creator)
    if len(matches) != 1:
        logger.info(
            f"[sms-group] Creator {creator} matches {len(matches)} circle(s); "
            f"not auto-linking {event.conversation_sid}"
        )
        return

    circle_id = str(matches[0]["circle_id"])
    circle = repo.get_circle_by_id(circle_id)
    channel = (circle.get("integration_config") or {}).get("channel") or "groupme"
    if channel != CHANNEL:
        # One chat per circle: a GroupMe circle doesn't get a group text
        # attached. TODO: text the creator 1:1 offering to switch.
        logger.info(
            f"[sms-group] {matches[0]['circle_name']} uses {channel}, not group text; "
            f"not linking {event.conversation_sid}"
        )
        return
    current = (circle.get("integration_config") or {}).get("sms_group") or {}
    if current.get("conversation_sid"):
        # Membership changed: a group text's participant set is fixed, so
        # adding someone starts a new thread and a new Conversation. Don't
        # silently replace the old link; that decision belongs to the
        # membership flow.
        logger.info(
            f"[sms-group] Circle {matches[0]['circle_name']} already linked to "
            f"{current['conversation_sid']}; not replacing with {event.conversation_sid}"
        )
        return

    # Twilio doesn't always send ProjectedAddress for an autocreated group
    # MMS, and lists the Take Five number among the group's addresses. Fall
    # back to the configured group number, and keep it out of participants:
    # participants are the people who must consent, and Take Five isn't one.
    projected = event.projected_address or os.getenv("TWILIO_GROUP_NUMBER")
    if not projected:
        logger.error("[sms-group] TWILIO_GROUP_NUMBER not set and no ProjectedAddress; @T5 replies won't work")
    participants = sorted({creator, *event.addresses} - {projected})

    repo.update_care_circle(circle_id, {"integration_config": {"sms_group": {
        "conversation_sid": event.conversation_sid,
        "created_by": creator,
        "projected_address": projected,
        "participants": participants,
        "status": STATUS_PENDING,
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }}})
    logger.info(f"[sms-group] Linked {event.conversation_sid} to circle {matches[0]['circle_name']} (pending consent)")


async def on_message_added(event: ConversationEvent) -> None:
    circle = repo.find_circle_by_sms_group_conversation(event.conversation_sid)
    if not circle:
        logger.info(f"[sms-group] Message in unlinked conversation {event.conversation_sid}; not stored")
        return

    cfg = circle.get("integration_config") or {}
    sms_group = cfg.get("sms_group") or {}
    if (cfg.get("channel") or "groupme") != CHANNEL or sms_group.get("status") != STATUS_ACTIVE:
        logger.info(
            f"[sms-group] Message in {circle['name']} while {sms_group.get('status')}; "
            f"not stored until all participants consent"
        )
        return

    projected = sms_group.get("projected_address") or os.getenv("TWILIO_GROUP_NUMBER")
    if event.author and projected and event.author == projected:
        return  # our own outbound post echoed back; already logged when sent

    if event.message_sid and repo.sms_group_message_exists(event.message_sid):
        return  # Twilio retry of a message we already stored (and answered)

    person = repo.find_circle_member_by_phone(str(circle["id"]), event.author) if event.author else None
    if not person:
        logger.warning(f"[sms-group] {event.author} isn't a member of {circle['name']}; storing without person")

    body = event.body
    if event.has_media and not body.strip():
        body = "[shared a photo]"

    msg = repo.log_message_for_circle(
        circle_id=str(circle["id"]),
        person_id=str(person["id"]) if person else None,
        body=body,
        raw_data={
            "conversation_sid": event.conversation_sid,
            "message_sid": event.message_sid,
            "author": event.author,
            "has_media": event.has_media,
        },
        channel=CHANNEL,
    )
    logger.info(f"[sms-group] Stored message from {person['name'] if person else event.author} in {circle['name']}")

    await run_post_storage_pipeline(
        message_id=str(msg["id"]),
        circle_id=str(circle["id"]),
        body=body,
        sender=person["name"] if person else (event.author or "unknown"),
        sent_at=msg["sent_at"],
        channel=CHANNEL,
    )

    if is_t5_command(body) and projected:
        # Background task: ask_with_tools can take longer than Twilio's
        # webhook timeout, and a timeout would trigger a retry.
        async def answer():
            from take_five.integrations.chat import CircleReplier  # avoid import cycle
            try:
                await handle_t5(
                    question=body.strip(),
                    circle_id=str(circle["id"]),
                    sender_person_id=str(person["id"]) if person else None,
                    channel=CHANNEL,
                    replier=CircleReplier(circle),
                )
            except Exception as e:
                logger.error(f"[sms-group] @T5 failed in {circle['name']}: {e}", exc_info=True)
        asyncio.create_task(answer())
    # TODO: media (fetch via the Conversations Media API, then images.py/attachments.py)


Handler = Callable[[ConversationEvent], Awaitable[None]]
_HANDLERS: dict[str, Handler] = {
    "onConversationAdded": on_conversation_added,
    "onMessageAdded": on_message_added,
}


async def handle_conversations_webhook(request: Request) -> Response:
    form = await request.form()
    validate_signature(request, form)
    event = parse_event(form)

    handler = _HANDLERS.get(event.event_type)
    if not handler:
        logger.info(f"[sms-group] Ignoring {event.event_type} for {event.conversation_sid}")
        return Response(status_code=200)

    try:
        await handler(event)
    except Exception as e:
        # Always 200: a non-2xx makes Twilio retry post-action events, and a
        # bug on our side shouldn't turn into a retry storm.
        logger.error(f"[sms-group] {event.event_type} handler failed for {event.conversation_sid}: {e}", exc_info=True)
    return Response(status_code=200)
