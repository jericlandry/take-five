"""
Consent for group-text circles (step 2 of claude/sms-group-onboarding-design.md).

The admin puts people on a circle's group-text roster
(integration_config.sms_group.roster) and sends each one a personal invite
link. The link opens sms-group-consent.html?t=<token> (the registered
campaign page, personalized), which calls these endpoints:

  GET  /consent/sms-group/{token}          page data: circle, who's confirmed
  POST /consent/sms-group/{token}/code     text a 6-digit code to their phone
  POST /consent/sms-group/{token}/confirm  {code, consented} -> sms_consents row

The code proves the person holds that phone (no consenting on someone
else's behalf). Take Five never texts anyone here except the code they just
asked for, from the auth number, same as login.

Invite tokens are signed (HMAC), not stored: person_id + circle_id + expiry.
"""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from take_five.auth import (
    OTP_LENGTH, OTP_MAX_ATTEMPTS, OTP_REQUEST_LIMIT, OTP_REQUEST_WINDOW_MINUTES,
    OTP_TTL_MINUTES, TWILIO_AUTH_FROM_NUMBER, _hash_otp,
)
from take_five.repository import repo

logger = logging.getLogger(__name__)

consent_router = APIRouter(prefix="/consent")

PROGRAM = "sms_group"
INVITE_TTL_DAYS = 14

# Must match the checkbox wording on website/sms-group-consent.html exactly.
# Change both together and bump the version, so stored consents keep
# pointing at the wording each person actually saw.
CONSENT_VERSION = "sms-group-2026-11-11"
CONSENT_TEXT = (
    "I agree to receive care circle text messages from Take Five Care, including "
    "updates shared by members of my care circle, care summaries, and reminders. "
    "Message frequency varies. Message and data rates may apply. Reply STOP to "
    "opt out at any time. Reply HELP for help."
)
CONSENT_TEXT_HASH = hashlib.sha256(CONSENT_TEXT.encode()).hexdigest()
CONSENT_PAGE_URL = "https://takefive.care/sms-group-consent.html"


# ─── Invite tokens ───

def _secret() -> bytes:
    s = os.getenv("CONSENT_TOKEN_SECRET")
    if not s:
        logger.error("[consent] CONSENT_TOKEN_SECRET not set")
        raise HTTPException(status_code=500, detail="Consent links aren't configured")
    return s.encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_invite_token(person_id: str, circle_id: str, ttl_days: int = INVITE_TTL_DAYS) -> str:
    payload = f"{person_id}.{circle_id}.{int(time.time()) + ttl_days * 86400}".encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:18]
    return f"{_b64(payload)}.{_b64(sig)}"


def invite_url(person_id: str, circle_id: str) -> str:
    return f"{CONSENT_PAGE_URL}?t={make_invite_token(str(person_id), str(circle_id))}"


def read_invite_token(token: str) -> tuple[str, str]:
    """Returns (person_id, circle_id) or raises 404 (bad) / 410 (expired)."""
    try:
        p, s = token.split(".")
        payload = _unb64(p)
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()[:18]
        if not hmac.compare_digest(expected, _unb64(s)):
            raise ValueError("bad signature")
        person_id, circle_id, exp = payload.decode().split(".")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="This link isn't valid")
    if int(exp) < time.time():
        raise HTTPException(status_code=410, detail="This link has expired. Ask your circle's admin for a new one.")
    return person_id, circle_id


# ─── Roster and consent state ───

def roster_ids(circle: dict) -> list:
    return [str(p) for p in (((circle or {}).get("integration_config") or {}).get("sms_group") or {}).get("roster", [])]


def consent_status(circle_id: str) -> dict:
    """person_id -> 'confirmed' | 'declined' | 'revoked' (no key = pending)."""
    out = {}
    for row in repo.get_current_sms_consents(circle_id, PROGRAM):
        pid = str(row["person_id"])
        if row.get("revoked_at"):
            out[pid] = "revoked"
        else:
            out[pid] = "confirmed" if row["consented"] else "declined"
    return out


def roster_ready(circle: dict) -> bool:
    """Everyone on the roster (2 to 9 people) has a current, unrevoked yes."""
    ids = roster_ids(circle)
    if not (2 <= len(ids) <= 9):
        return False
    status = consent_status(str(circle["id"]))
    return all(status.get(pid) == "confirmed" for pid in ids)


async def on_consent_recorded(circle: dict) -> None:
    """Step 4 hook: when the roster is fully confirmed, create the group and
    post the welcome. For now this only logs readiness."""
    if roster_ready(circle):
        logger.info(f"[consent] {circle['name']}: everyone on the roster has confirmed; ready to start the group")


def _load(token: str) -> tuple[dict, dict]:
    person_id, circle_id = read_invite_token(token)
    circle = repo.get_circle_by_id(circle_id)
    person = repo.get_person_by_id(person_id)
    if not circle or not person or person_id not in roster_ids(circle):
        raise HTTPException(status_code=404, detail="This invitation is no longer active. Ask your circle's admin.")
    if not person.get("phone"):
        raise HTTPException(status_code=400, detail="There's no mobile number on file for you. Ask your circle's admin to add one.")
    return person, circle


def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def _mask(phone: str) -> str:
    return f"•••-{phone[-4:]}" if phone else ""


# ─── Endpoints ───

@consent_router.get("/sms-group/{token}")
async def consent_page_data(token: str):
    person, circle = _load(token)
    status = consent_status(str(circle["id"]))
    people = {str(p["id"]): p for p in repo.fetch_circle_roster(str(circle["id"]))}
    roster = [
        {"first_name": _first(people[pid]["member_name"]) if pid in people else "",
         "status": status.get(pid, "pending"),
         "is_you": pid == str(person["id"])}
        for pid in roster_ids(circle)
    ]
    sg = (circle.get("integration_config") or {}).get("sms_group") or {}
    return {
        "first_name": _first(person["name"]),
        "masked_phone": _mask(person["phone"]),
        "circle_name": circle["name"],
        "your_status": status.get(str(person["id"]), "pending"),
        "roster": roster,
        "group_live": sg.get("status") == "active",
        "consent_text": CONSENT_TEXT,
        "consent_version": CONSENT_VERSION,
        "group_number": os.getenv("TWILIO_GROUP_NUMBER"),
    }


@consent_router.post("/sms-group/{token}/code")
async def consent_send_code(token: str):
    from take_five.integrations.twilio import send_sms  # avoid import cycle
    person, _ = _load(token)
    phone = person["phone"]
    window_start = datetime.now(timezone.utc) - timedelta(minutes=OTP_REQUEST_WINDOW_MINUTES)
    if repo.count_recent_otp_requests(phone, window_start) >= OTP_REQUEST_LIMIT:
        raise HTTPException(status_code=429, detail="Too many codes requested. Try again in a few minutes.")
    code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
    repo.create_otp_code(phone, _hash_otp(phone, code), expires_at)
    try:
        send_sms(phone, f"Your Take Five code is {code}. It expires in {OTP_TTL_MINUTES} minutes.",
                 from_number=TWILIO_AUTH_FROM_NUMBER)
    except Exception as e:
        logger.error(f"[consent] Failed to send code to {_mask(phone)}: {e}")
        raise HTTPException(status_code=502, detail="Couldn't send the code. Try again.")
    return {"sent_to": _mask(phone)}


class ConfirmRequest(BaseModel):
    code: str
    consented: bool


@consent_router.post("/sms-group/{token}/confirm")
async def consent_confirm(token: str, body: ConfirmRequest, request: Request):
    person, circle = _load(token)
    phone = person["phone"]
    otp = repo.get_latest_unconsumed_otp(phone)
    if not otp or otp["attempts"] >= OTP_MAX_ATTEMPTS or otp["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="That code has expired. Request a new one.")
    if not hmac.compare_digest(otp["code_hash"], _hash_otp(phone, body.code.strip())):
        repo.increment_otp_attempts(str(otp["id"]))
        raise HTTPException(status_code=400, detail="That code doesn't match. Check it and try again.")
    repo.consume_otp_code(str(otp["id"]))

    forwarded = request.headers.get("x-forwarded-for", "")
    repo.record_sms_consent(
        person_id=str(person["id"]), circle_id=str(circle["id"]), phone=phone,
        program=PROGRAM, consented=body.consented,
        consent_version=CONSENT_VERSION, consent_text_hash=CONSENT_TEXT_HASH,
        verified_via="otp", page_url=CONSENT_PAGE_URL,
        ip=forwarded.split(",")[0].strip() or (request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    )
    logger.info(f"[consent] {person['name']} {'confirmed' if body.consented else 'declined'} group text for {circle['name']}")
    await on_consent_recorded(repo.get_circle_by_id(str(circle["id"])))
    return {"status": "confirmed" if body.consented else "declined"}
