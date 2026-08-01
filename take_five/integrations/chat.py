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
import logging

from take_five.integrations import groupme

logger = logging.getLogger(__name__)


async def setup_chat_circle(circle_id: str) -> dict:
    """
    Create a chat group/bot for a care circle on its configured chat
    platform, and lock down member management to admin-only so the Take
    Five admin app becomes the sole path for adding chat members going
    forward (a GroupMe-native member add is blocked once this runs).

    Only GroupMe is implemented today; this is the dispatch point where a
    second platform plugs in without touching callers.
    """
    # No per-circle channel selection yet — see module docstring.
    return await groupme.setup_groupme_circle(circle_id)


async def add_person_to_chat(circle_id: str, person_id: str) -> dict:
    """
    Add a specific person to a circle's chat platform. This is an explicit,
    per-person action distinct from circle membership — some circle members
    (e.g. the senior, or a caregiver who'd rather just text a number) may
    intentionally never be added to the group chat itself. See migration
    009_chat_membership.sql and Trello #59.
    """
    return await groupme.add_person_to_groupme(circle_id, person_id)
