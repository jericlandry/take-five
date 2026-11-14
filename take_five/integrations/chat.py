"""
Chat integration dispatcher.

Take Five is designed to meet families where they already communicate —
GroupMe today, WhatsApp planned (see takefive-pilot-v2.html). This module is
the seam between that product intent and the code: callers (main.py routes,
admin actions) should import setup_chat_circle() / add_person_to_chat() from
here, never reach directly into take_five.integrations.groupme.

Today every circle implicitly uses GroupMe — there's no multi-platform
routing yet, and care_circles.integration_config has no 'channel' key. When
WhatsApp support is added, integration_config should gain a 'channel' key
and the dispatch functions below should branch on it. Until then, everything
routes to GroupMe by construction, and that's fine: the point of this module
existing now is that adding the second platform later touches only this file
(new branch + a new whatsapp.py) instead of every call site across the app.

See Trello #59 discussion, 2026-08-01, for the design context.
"""
import asyncio
import logging
import os
import re
from typing import Optional

from take_five.integrations import groupme
from take_five.repository import repo

logger = logging.getLogger(__name__)


async def setup_chat_circle(circle_id: str, admin_person_id: str = None) -> dict:
    """
    Create a chat group/bot for a care circle on its configured chat
    platform, and lock down member management to admin-only so the Take
    Five admin app becomes the sole path for adding chat members going
    forward (a GroupMe-native member add is blocked once this runs).

    admin_person_id: the person whose account should create (and own) the
    group — see groupme.setup_groupme_circle() / Trello #39. Optional and
    defaults to None (falls back to GROUPME_USER_ACCESS_TOKEN) purely for
    backward compatibility with callers not yet updated to pass it.

    Only GroupMe is implemented today; this is the dispatch point where a
    second platform plugs in without touching callers.
    """
    # No per-circle channel selection yet — see module docstring.
    return await groupme.setup_groupme_circle(circle_id, admin_person_id=admin_person_id)


async def add_person_to_chat(circle_id: str, person_id: str) -> dict:
    """
    Add a specific person to a circle's chat platform. This is an explicit,
    per-person action distinct from circle membership — some circle members
    (e.g. the senior, or a caregiver who'd rather just text a number) may
    intentionally never be added to the group chat itself. See migration
    009_chat_membership.sql and Trello #59.
    """
    return await groupme.add_person_to_groupme(circle_id, person_id)


async def remove_person_from_chat(circle_id: str, person_id: str) -> dict:
    """
    Remove a specific person from a circle's chat platform. Does not affect
    circle membership itself — only the chat presence (see migration
    009_chat_membership.sql). Mirrors add_person_to_chat.
    """
    return await groupme.remove_person_from_groupme(circle_id, person_id)


# ─── One chat per circle, and one way to post to it (Nov 2026) ───
#
# A circle talks on exactly one chat: integration_config['channel'] is
# "groupme" (default, every circle before Nov 2026) or "sms_group" (native
# group text on the Take Five Care LLC number; see twilio_conversations.py).
# No bridging between the two (decided 2026-11-13).
#
# Everything Take Five posts into a circle's chat goes through
# deliver_to_circle(): @T5 answers, relayed aide texts and emails, weekly
# digests, engagement prompts, prep packets, photo-read replies. Callers
# never pick a platform.

CHANNEL_GROUPME = "groupme"
CHANNEL_SMS_GROUP = "sms_group"

_SENTINEL_PREFIXES = ("[SAVED:", "[PATCHED:")


def circle_channel(circle: dict) -> str:
    return ((circle or {}).get("integration_config") or {}).get("channel") or CHANNEL_GROUPME


def chat_ready(circle: dict) -> bool:
    """True when the circle's chat can receive a post right now."""
    cfg = (circle or {}).get("integration_config") or {}
    if circle_channel(circle) == CHANNEL_SMS_GROUP:
        sg = cfg.get("sms_group") or {}
        return sg.get("status") == "active" and bool(sg.get("conversation_sid"))
    return bool(cfg.get("groupme_bot_id"))


def strip_sentinels(text: str) -> str:
    """Drop [SAVED:]/[PATCHED:] lines: users never see them, but the logged
    body keeps them so Claude can read its own state on later turns."""
    return "\n".join(
        line for line in (text or "").splitlines()
        if not line.startswith(_SENTINEL_PREFIXES)
    ).strip()


def clean_for_sms_group(text: str) -> str:
    """Deterministic plain-text cleanup for a group text: no sentinels, no
    em dashes (prompt-instructed everywhere, but the model doesn't always
    comply), no markdown emphasis/headings."""
    t = strip_sentinels(text)
    t = re.sub(r"\s*\u2014\s*", ", ", t)          # em dash -> comma
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)    # **bold** / __bold__
    t = re.sub(r"(?m)^#{1,6}\s+", "", t)             # headings
    t = re.sub(r"(?m)^\s*[*\u2022]\s+", "- ", t)   # bullets -> plain dashes
    return t.strip()


async def deliver_to_circle(
    circle: dict,
    text: str,
    *,
    msg_type: str = "agent_note",
    log: bool = True,
    image: Optional[tuple] = None,
) -> bool:
    """
    Post to whichever chat this circle uses, and (by default) log it once as
    an outbound message of msg_type. image is (bytes, content_type); each
    platform hosts it its own way. Returns False when the circle's chat isn't
    ready (no GroupMe bot / group text not active) or nothing was sent.

    log=False is for posts already logged elsewhere (prep packets log
    themselves as message_type='prep_packet' in generate_prep_packet).
    """
    if not circle or (not text and not image):
        return False
    if not chat_ready(circle):
        logger.warning(f"[chat] {circle.get('name')}: {circle_channel(circle)} chat not ready; not posted")
        return False

    cfg = circle.get("integration_config") or {}
    channel = circle_channel(circle)

    if channel == CHANNEL_SMS_GROUP:
        from take_five.integrations.twilio_conversations import send_to_conversation  # avoid import cycle
        sg = cfg["sms_group"]
        author = sg.get("projected_address") or os.getenv("TWILIO_GROUP_NUMBER")
        sent = await send_to_conversation(sg["conversation_sid"], author, text or "", image=image)
        raw = {"source": "t5_bot", "conversation_sid": sg["conversation_sid"]}
    else:
        bot_id = cfg["groupme_bot_id"]
        picture_url = await groupme.upload_image_to_groupme(*image) if image else None
        if image and not picture_url:
            logger.error(f"[chat] Image upload to GroupMe failed for {circle.get('name')}; posting text only")
        sent = await groupme.send_message_async(bot_id, strip_sentinels(text or ""), picture_url=picture_url)
        raw = {"source": "t5_bot", "bot_id": bot_id}
        if picture_url:
            raw["picture_url"] = picture_url

    if log and sent:
        try:
            repo.log_message_for_circle(
                circle_id=str(circle["id"]), person_id=None,
                body=text or "[image]", msg_type=msg_type, direction="outbound",
                raw_data=raw, channel=channel,
            )
        except Exception as e:
            logger.error(f"[chat] Failed to log {msg_type} for {circle.get('name')}: {e}")
    return bool(sent)


def deliver_to_circle_sync(circle: dict, text: str, **kwargs) -> bool:
    """For the scheduled scripts (main_summary.py, main_engagement.py's
    sync paths), which aren't running an event loop."""
    return asyncio.run(deliver_to_circle(circle, text, **kwargs))


class CircleReplier:
    """take_five.t5_commands.Replier for any circle, whatever its chat:
    reply() posts and logs an agent_note; post() posts without logging."""

    def __init__(self, circle: dict):
        self.circle = circle

    async def reply(self, text: str) -> None:
        await deliver_to_circle(self.circle, text)

    async def post(self, text: str) -> None:
        await deliver_to_circle(self.circle, text, log=False)
