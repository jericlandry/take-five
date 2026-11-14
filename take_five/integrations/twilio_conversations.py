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


def _visible(text: str) -> str:
    """Drop the [SAVED:]/[PATCHED:] sentinel lines, same as groupme_reply:
    users never see them, but the logged body keeps them so Claude can read
    its own state on later turns."""
    return "\n".join(
        line for line in (text or "").splitlines()
        if not line.startswith("[SAVED:") and not line.startswith("[PATCHED:")
    ).strip()


class ConversationReplier:
    """take_five.t5_commands.Replier for a circle's native group text.
    Posts into the Twilio Conversation as the Take Five number (for an
    autocreated group MMS the projected address is the only valid author).
    Our own posts come back through onMessageAdded and are skipped there."""

    def __init__(self, circle_id: str, conversation_sid: str, author: str):
        self.circle_id = circle_id
        self.conversation_sid = conversation_sid
        self.author = author

    async def _send(self, text: str) -> None:
        visible = _visible(text)
        if not visible:
            return
        for chunk in split_for_groupme(visible, limit=SMS_GROUP_MAX_CHARS):
            await asyncio.to_thread(
                _client().conversations.v1.conversations(self.conversation_sid).messages.create,
                author=self.author, body=chunk,
            )

    async def reply(self, text: str) -> None:
        await self._send(text)
        try:
            repo.log_message_for_circle(
                circle_id=self.circle_id, person_id=None, body=text,
                msg_type="agent_note", direction="outbound",
                raw_data={"source": "t5_bot", "conversation_sid": self.conversation_sid},
                channel=CHANNEL,
            )
        except Exception as e:
            logger.error(f"[sms-group] Failed to log T5 reply: {e}")

    async def post(self, text: str) -> None:
        await self._send(text)


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

    repo.update_care_circle(circle_id, {"integration_config": {"sms_group": {
        "conversation_sid": event.conversation_sid,
        "created_by": creator,
        "projected_address": event.projected_address,
        "participants": sorted({creator, *event.addresses}),
        "status": STATUS_PENDING,
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }}})
    logger.info(f"[sms-group] Linked {event.conversation_sid} to circle {matches[0]['circle_name']} (pending consent)")


async def on_message_added(event: ConversationEvent) -> None:
    circle = repo.find_circle_by_sms_group_conversation(event.conversation_sid)
    if not circle:
        logger.info(f"[sms-group] Message in unlinked conversation {event.conversation_sid}; not stored")
        return

    sms_group = (circle.get("integration_config") or {}).get("sms_group") or {}
    if sms_group.get("status") != STATUS_ACTIVE:
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
            try:
                await handle_t5(
                    question=body.strip(),
                    circle_id=str(circle["id"]),
                    sender_person_id=str(person["id"]) if person else None,
                    channel=CHANNEL,
                    replier=ConversationReplier(str(circle["id"]), event.conversation_sid, projected),
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
