"""
SendGrid Inbound Parse integration -- lets circle members email care
updates in, the same way they already text them via GroupMe/SMS. See
main.py's /sendgrid/inbound route for the webhook entry point.

Design summary (chat history has the full reasoning):
  - One inbound address PER CIRCLE (not per ensemble), matching how
    messages.circle_id already works and how GroupMe already gives each
    circle its own group chat.
  - The address's local-part is a base62 encoding of circle_id itself --
    not a stored slug -- so it can't drift out of sync with an editable
    circle name, requires no new column/migration, and doesn't leak the
    raw UUID (an internal primary key) into email headers/contact
    lists/logs.
  - Display name (what the recipient actually sees/saves as a contact) is
    set separately in the From header when Take Five sends mail --
    "{ensemble.name} - {circle.name}" -- fully decoupled from the address.
  - Sender identity is verified against people.email scoped to the
    TARGET circle's membership (not just anywhere in the ensemble) via
    repo.find_person_in_circle_by_email. Unrecognized senders are held
    (logged, not ingested) rather than silently accepted.
  - Body text is cleaned of quoted-reply/signature noise before storage
    AND before GroupMe relay -- see clean_email_body(). Tried three
    approaches before landing here (full comparison in chat history):
    mailgun/talon (the most well-known library for this) doesn't install
    on modern Python -- its cchardet dependency fails to build, references
    longintrepr.h, a CPython internal header removed in 3.11+, confirmed
    by trying it directly, not assumed. A hand-rolled regex version worked
    but is a reimplementation of a notoriously fiddly problem. Landed on
    email_reply_parser (Zapier's port) -- zero dependencies, installs
    clean, and empirically the most correct of everything tried on a set
    of realistic Gmail/Outlook/Apple Mail reply samples (mail-parser-reply
    was also tried: richer multi-language support we don't need yet, but
    its convenience API reattaches the detected signature -- a real
    gotcha -- and it left a quoted line behind on the plain "> " case
    that email_reply_parser handled cleanly).
  - Relayed into GroupMe the same way SMS is (take_five/integrations/
    twilio.py's _process_caregiver_sms): raw cleaned text, no LLM
    paraphrasing, "{name} (via Take Five): {body}" attribution -- so an
    email update gets the same real-time visibility as a text, not
    second-class treatment.
"""
import asyncio
import logging
import os
import uuid
from email.utils import parseaddr
from typing import Optional

from email_reply_parser import EmailReplyParser
from fastapi import Request

from take_five.integrations.groupme import groupme_reply
from take_five.pipeline import run_post_storage_pipeline
from take_five.repository import repo

logger = logging.getLogger(__name__)

# --- Address encode/decode -------------------------------------------------

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(_ALPHABET)
# A UUID is 128 bits; 62**21 < 2**128 < 62**22, so 22 base62 digits are
# always enough and sometimes more than needed. Fixed-width, left-padded
# with the alphabet's zero digit, so every circle's address is the same
# length -- encoding a UUID with leading zero bytes otherwise silently
# produces a SHORTER string for that one circle, which looks like a bug
# rather than an artifact of the encoding.
_ENCODED_WIDTH = 22


def encode_circle_id(circle_id: str) -> str:
    """
    circle_id (UUID) -> local-part of its inbound email address, e.g.
    6efcc887-98a2-4ce0-b5cb-719a62a80cfd -> 3NQg3GIyRpdVxsdRGNYmZZ.

    Pure function of circle_id, no DB round-trip and no new stored
    state -- this can run in either direction (mint an address to display
    in the admin UI / outbound From header, or decode one from an inbound
    webhook) without anything else needing to stay in sync.
    """
    num = uuid.UUID(str(circle_id)).int
    if num == 0:
        digits = _ALPHABET[0]
    else:
        chars = []
        while num:
            num, rem = divmod(num, _BASE)
            chars.append(_ALPHABET[rem])
        digits = "".join(reversed(chars))
    return digits.rjust(_ENCODED_WIDTH, _ALPHABET[0])


def decode_circle_local_part(local_part: str) -> Optional[str]:
    """
    Inverse of encode_circle_id. Returns None (never raises) on malformed
    input -- an inbound webhook can receive local-parts that were never a
    valid circle address (spam, bounces, someone guessing), and the caller
    should treat that as "no matching circle", not a 500.
    """
    local_part = (local_part or "").strip()
    if not local_part:
        return None
    num = 0
    for ch in local_part:
        idx = _ALPHABET.find(ch)
        if idx == -1:
            return None
        num = num * _BASE + idx
    try:
        return str(uuid.UUID(int=num))
    except (ValueError, OverflowError):
        return None


def circle_inbound_address(circle_id: str) -> str:
    """Full inbound address for a circle -- e.g. for the admin UI to show
    the family what to save as a contact, or as an outbound Reply-To."""
    domain = os.getenv("INBOUND_EMAIL_DOMAIN", "combo.takefive.care")
    return f"{encode_circle_id(circle_id)}@{domain}"


# --- Quoted-reply / signature cleanup ---------------------------------------
#
# email_reply_parser (Zapier's port) -- see module docstring for why this
# won out over mailgun/talon (broken install on modern Python) and
# mail-parser-reply (reattaches signatures via its convenience API, and
# less accurate on plain "> " quoting in testing).


def clean_email_body(text: str) -> str:
    """
    Strip quoted-reply chains and signatures from an inbound email body, so
    what gets stored/relayed/digested/signal-scanned is just the sender's
    own words -- not the entire prior thread they replied underneath. The
    uncleaned original is preserved separately in the message's raw_data
    for audit/debugging; nothing is destroyed, this only affects what
    counts as the message's body.
    """
    if not text:
        return ""
    parsed = EmailReplyParser.parse_reply(text)
    return (parsed or text).strip()


# --- Webhook signature verification -----------------------------------------
#
# TODO before going live: SendGrid's Inbound Parse signature verification
# is opt-in -- you attach a "webhook security policy" (ECDSA and/or OAuth)
# to the Parse setting via a separate API call, and that setup response is
# where the exact header names get confirmed. Docs describe the mechanism
# (ECDSA, public key you download and store) but not it in enough
# specific, Parse-scoped detail to hardcode header names here with
# confidence -- verify directly against the security-policy creation
# response when this gets configured, rather than trusting the names
# below blindly. `pip install ecdsa` if going this route.
#
# Deliberately fails OPEN (skips verification, logs a warning) rather than
# rejecting every request when unconfigured, since sender-email-vs-
# circle-membership matching below is the real access control regardless
# of whether the transport-level signature is also wired up -- an
# unconfigured signature check should degrade to "no extra layer yet",
# not "webhook is broken".

_SENDGRID_INBOUND_PUBLIC_KEY = os.getenv("SENDGRID_INBOUND_PUBLIC_KEY")


def _verify_signature(request: Request, raw_body: bytes) -> bool:
    if not _SENDGRID_INBOUND_PUBLIC_KEY:
        logger.warning(
            "[sendgrid-email] SENDGRID_INBOUND_PUBLIC_KEY not configured -- "
            "skipping signature verification. Fine for initial testing, "
            "but confirm the actual header names/payload shape from the "
            "Parse security-policy setup response before relying on this "
            "in production."
        )
        return True

    import base64
    import ecdsa

    signature = request.headers.get("X-Twilio-Email-Event-Webhook-Signature")
    timestamp = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp")
    if not signature or not timestamp:
        logger.warning("[sendgrid-email] Missing signature headers on inbound request")
        return False

    try:
        public_key = ecdsa.VerifyingKey.from_pem(_SENDGRID_INBOUND_PUBLIC_KEY)
        payload = timestamp.encode("utf-8") + raw_body
        return public_key.verify(
            base64.b64decode(signature), payload,
            hashfunc=__import__("hashlib").sha256,
            sigdecode=ecdsa.util.sigdecode_der,
        )
    except Exception as e:
        logger.warning(f"[sendgrid-email] Signature verification failed: {e}")
        return False


# --- Webhook handler ---------------------------------------------------------

def _extract_email(header_value: str) -> str:
    """'Dad <example@gmail.com>' -> 'example@gmail.com'. parseaddr returns
    ('', '') on totally unparseable input rather than raising."""
    return (parseaddr(header_value or "")[1] or "").strip()


async def handle_inbound_email(request: Request) -> dict:
    raw_body = await request.body()

    if not _verify_signature(request, raw_body):
        # A bad signature is a real (if mild) security signal, not a
        # transient error -- 403 rather than a swallowed 200, so it's
        # distinguishable in logs/metrics from "recognized sender" drops.
        return {"status": "rejected", "reason": "signature verification failed"}

    form = await request.form()
    to_field = form.get("to", "")
    from_field = form.get("from", "")
    subject = form.get("subject", "")
    raw_text = (form.get("text") or "").strip()
    body_text = clean_email_body(raw_text)

    to_email = _extract_email(to_field)
    local_part = to_email.split("@")[0] if "@" in to_email else to_email
    circle_id = decode_circle_local_part(local_part)

    if not circle_id:
        logger.warning(f"[sendgrid-email] Unrecognized recipient local-part: {local_part!r}")
        return {"status": "ok"}  # not a valid circle address -- drop, don't retry

    circle = repo.get_circle_by_id(circle_id)
    if not circle or circle.get("status") != "active":
        logger.warning(f"[sendgrid-email] No active circle for decoded id {circle_id}")
        return {"status": "ok"}

    sender_email = _extract_email(from_field)
    person = repo.find_person_in_circle_by_email(circle_id, sender_email)
    if not person:
        # Held, not ingested -- matches the project's stance that an
        # unrecognized sender shouldn't be silently written into a
        # family's care record. No retry needed; this isn't transient.
        logger.warning(
            f"[sendgrid-email] Sender {sender_email!r} is not a member of "
            f"circle {circle_id} -- message held, not ingested."
        )
        return {"status": "ok"}

    if not body_text:
        logger.info(f"[sendgrid-email] Empty body (after cleanup) from {sender_email} to circle {circle_id} -- dropped")
        return {"status": "ok"}

    row = repo.log_message(
        circle_ext_id=circle["external_id"],
        person_ext_id=None,
        person_id=person["id"],
        body=body_text,
        msg_type="inbound",
        direction="inbound",
        raw_data={"subject": subject, "from": from_field, "to": to_field, "raw_text": raw_text},
        channel="email",
    )

    # Same fire-and-forget pattern as every other inbound channel
    # (GroupMe, SMS, reference backfill) -- embeddings + clinical signal
    # detection run async, response to SendGrid doesn't wait on them.
    asyncio.create_task(run_post_storage_pipeline(
        message_id=str(row["id"]),
        circle_id=str(circle_id),
        body=body_text,
        sender=person["name"],
        sent_at=row["sent_at"],
        channel="email",
    ))

    # Relay to GroupMe -- same treatment as an SMS caregiver update (see
    # take_five/integrations/twilio.py's _process_caregiver_sms): cleaned
    # text as written, no LLM paraphrasing, so an email update shows up
    # live in the family's chat the same way a text or GroupMe post would,
    # rather than silently waiting for the weekly digest.
    bot_id = (circle.get("integration_config") or {}).get("groupme_bot_id")
    groupme_ext_id = circle.get("external_id")
    if bot_id and groupme_ext_id:
        relay_text = f"{person['name']} (via Take Five): {body_text}"
        asyncio.create_task(groupme_reply(bot_id, relay_text, groupme_ext_id))

    logger.info(f"[sendgrid-email] Logged message from {person['name']} to circle {circle_id}")
    return {"status": "ok"}
