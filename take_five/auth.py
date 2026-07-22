import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from take_five.integrations.twilio import send_sms
from take_five.repository import repo
from take_five.schemas import RequestOtpRequest, SelectAccountRequest, VerifyOtpRequest

logger = logging.getLogger(__name__)

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_REQUEST_LIMIT = 5
OTP_REQUEST_WINDOW_MINUTES = 15
SESSION_TTL_DAYS = 30

# Dedicated number for OTP codes — kept separate from TWILIO_FROM_NUMBER
# (used for care-update invites/relay) so a reply to a login code can't be
# misrouted into the inbound care-update webhook on the shared number.
TWILIO_AUTH_FROM_NUMBER = os.getenv("TWILIO_AUTH_FROM_NUMBER")

auth_router = APIRouter(prefix="/auth")

# ─── ACCOUNT SELECTION (one phone → multiple people) ───
#
# The same phone can belong to more than one person record (a shared
# household phone, a tester playing multiple roles) — the same ambiguity
# find_active_sms_members_by_phone already handles for inbound SMS via a
# "which circle is this for" disambiguation. This mirrors that: in-memory,
# not a DB table, short-lived (5 min), low-volume. Won't survive a process
# restart or multiple workers — acceptable for now given how rarely it
# fires, but worth revisiting if the platform moves to multiple app
# instances (same tradeoff already accepted for the SMS version).
_PENDING_SELECTION_TTL_SECONDS = 5 * 60
_pending_account_selections: dict[str, dict] = {}


def _stash_pending_selection(candidates: list) -> str:
    ticket = secrets.token_urlsafe(24)
    _pending_account_selections[ticket] = {
        'candidates': candidates,
        'expires_at': time.time() + _PENDING_SELECTION_TTL_SECONDS,
    }
    return ticket


def _get_pending_selection(ticket: str) -> Optional[dict]:
    pending = _pending_account_selections.get(ticket)
    if not pending:
        return None
    if time.time() > pending['expires_at']:
        del _pending_account_selections[ticket]
        return None
    return pending


# --- Phone normalization ---

_DIGITS_RE = re.compile(r"\D")


def normalize_phone(raw: str) -> str:
    """Normalize to E.164 for US numbers (pilot is US-only per the existing
    Twilio setup). Raises 400 on anything that isn't a plausible US number."""
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    raise HTTPException(status_code=400, detail="Invalid phone number")


# --- Hashing helpers (codes/tokens are stored hashed, never plaintext) ---

def _hash_otp(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def person_payload(person: dict) -> dict:
    return {
        "id": str(person["person_id"]),
        "name": person["person_name"],
        "email": person["email"],
        "phone": person["phone"],
        "aliases": person["aliases"] or [],
        "notes": person["notes"],
        "date_of_birth": str(person["date_of_birth"]) if person["date_of_birth"] else None,
    }


def ensemble_payload(person: dict) -> dict:
    return {
        "id": str(person["ensemble_id"]),
        "name": person["ensemble_name"],
        "plan": person["ensemble_plan"],
        "status": person["ensemble_status"],
    }


# --- Routes: OTP request / verify ---

@auth_router.post("/otp/request")
async def request_otp(body: RequestOtpRequest):
    phone = normalize_phone(body.phone)
    people = repo.lookup_people_by_phone(phone)
    # Always return 200 regardless of match — don't let this endpoint be
    # used to enumerate which phone numbers are registered. Only send an
    # SMS if a match exists.
    if people:
        window_start = datetime.now(timezone.utc) - timedelta(minutes=OTP_REQUEST_WINDOW_MINUTES)
        if repo.count_recent_otp_requests(phone, window_start) >= OTP_REQUEST_LIMIT:
            raise HTTPException(status_code=429, detail="Too many codes requested. Try again later.")
        code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)
        repo.create_otp_code(phone, _hash_otp(phone, code), expires_at)
        try:
            if not TWILIO_AUTH_FROM_NUMBER:
                raise RuntimeError("TWILIO_AUTH_FROM_NUMBER not configured")
            send_sms(
                phone, f"Your Take Five code is {code}. It expires in {OTP_TTL_MINUTES} minutes.",
                from_number=TWILIO_AUTH_FROM_NUMBER,
            )
        except Exception as e:
            logger.error(f"[auth] Failed to send OTP to {phone}: {e}")
            raise HTTPException(status_code=500, detail="Failed to send verification code")
        logger.info(f"[auth] OTP sent to {phone} ({len(people)} matching account(s))")
    else:
        logger.info(f"[auth] OTP requested for unregistered phone {phone}")
    return {"status": "ok"}


def _issue_session(person: dict) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    repo.create_session(str(person["person_id"]), _hash_token(token), expires_at)
    logger.info(
        f"[auth] Login: {person['person_name']} ({person['person_id']}) "
        f"-> {person['ensemble_name']} ({person['ensemble_id']}), role={person['user_role']}"
    )
    return {
        "session_token": token,
        "person": person_payload(person),
        "ensemble": ensemble_payload(person),
        "user_role": person["user_role"],
    }


@auth_router.post("/otp/verify")
async def verify_otp(body: VerifyOtpRequest):
    phone = normalize_phone(body.phone)
    otp = repo.get_latest_unconsumed_otp(phone)
    if not otp or otp["attempts"] >= OTP_MAX_ATTEMPTS:
        logger.warning(f"[auth] Verify failed for {phone}: no valid code (expired, missing, or max attempts hit)")
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    if not hmac.compare_digest(otp["code_hash"], _hash_otp(phone, body.code)):
        repo.increment_otp_attempts(str(otp["id"]))
        logger.warning(f"[auth] Verify failed for {phone}: wrong code (attempt {otp['attempts'] + 1}/{OTP_MAX_ATTEMPTS})")
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    repo.consume_otp_code(str(otp["id"]))

    people = repo.lookup_people_by_phone(phone)
    if not people:
        logger.warning(f"[auth] Verify succeeded for {phone} but no matching person found")
        raise HTTPException(status_code=404, detail="No account found for that phone number")

    if len(people) > 1:
        ticket = _stash_pending_selection(people)
        logger.info(f"[auth] {phone} resolved to {len(people)} accounts — awaiting selection")
        return {
            "status": "select_account",
            "selection_ticket": ticket,
            "candidates": [
                {
                    "person_id": str(p["person_id"]),
                    "name": p["person_name"],
                    "ensemble_name": p["ensemble_name"],
                }
                for p in people
            ],
        }

    return _issue_session(people[0])


@auth_router.post("/otp/select-account")
async def select_account(body: SelectAccountRequest):
    """
    Second step when /auth/otp/verify found more than one person on the same
    phone — mirrors the SMS "which circle is this for" disambiguation, but
    for the web login flow: the client shows the candidate list, the user
    picks one, and this exchanges (ticket, person_id) for an actual session.
    """
    pending = _get_pending_selection(body.selection_ticket)
    if not pending:
        logger.warning("[auth] Account selection failed: ticket expired or unknown")
        raise HTTPException(status_code=401, detail="Selection expired. Please request a new code.")
    match = next(
        (p for p in pending['candidates'] if str(p["person_id"]) == body.person_id), None
    )
    if not match:
        logger.warning(f"[auth] Account selection failed: person_id {body.person_id} not in candidate list")
        raise HTTPException(status_code=400, detail="Invalid selection")
    del _pending_account_selections[body.selection_ticket]
    return _issue_session(match)


_session_scheme = HTTPBearer(auto_error=False)


@auth_router.post("/logout")
async def logout(credentials: Optional[HTTPAuthorizationCredentials] = Security(_session_scheme)):
    """Revoke the session server-side, not just forget it client-side."""
    if credentials is not None:
        token_hash = _hash_token(credentials.credentials)
        session = repo.get_session_by_token_hash(token_hash)
        repo.revoke_session(token_hash)
        if session:
            person = repo.get_person_by_id(str(session["person_id"]))
            name = person["name"] if person else session["person_id"]
            logger.info(f"[auth] Logout: {name} ({session['person_id']})")
    return {"status": "ok"}


# --- Dependency: resolve the caller from a session bearer token ---


async def get_current_person(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_session_scheme),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing session token")
    token_hash = _hash_token(credentials.credentials)
    session = repo.get_session_by_token_hash(token_hash)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    # Sliding 30-day window: every authenticated request pushes expiry out.
    repo.touch_session(str(session["id"]), datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS))
    person = repo.get_person_with_membership(str(session["person_id"]))
    if not person:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    return person


async def require_admin(person: dict = Depends(get_current_person)) -> dict:
    if person["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return person


# --- Dependency factory: enforce that path-param resources belong to the caller's ensemble ---

def _resolve_owner_ensemble(resource_type: str, resource_id: str) -> Optional[str]:
    if resource_type == "ensemble":
        ensemble = repo.get_ensemble(resource_id)
        return str(ensemble["id"]) if ensemble else None
    if resource_type == "circle":
        circle = repo.get_circle_by_id(resource_id)
        return str(circle["ensemble_id"]) if circle else None
    if resource_type == "person":
        target = repo.get_person_by_id(resource_id)
        return str(target["ensemble_id"]) if target else None
    if resource_type == "clinical_record":
        record = repo.get_clinical_record_by_id(resource_id)
        if not record:
            return None
        target = repo.get_person_by_id(str(record["person_id"]))
        return str(target["ensemble_id"]) if target else None
    raise ValueError(f"Unknown resource type: {resource_type}")


def require_ensemble_scope(checks: list, admin_only: bool = False):
    """
    Returns a FastAPI dependency that:
      1. Resolves the caller via get_current_person.
      2. For each (resource_type, path_param_name) pair, resolves that path
         param's owning ensemble_id and verifies it equals the caller's own
         ensemble_id — 404 if the resource doesn't exist, 403 on cross-tenant
         access.
      3. If admin_only, verifies caller.user_role == 'admin'.
    resource_type is one of 'ensemble' | 'circle' | 'person' | 'clinical_record'.
    """
    async def _dep(request: Request, person: dict = Depends(get_current_person)) -> dict:
        for resource_type, param_name in checks:
            resource_id = request.path_params.get(param_name)
            owner_ensemble_id = _resolve_owner_ensemble(resource_type, resource_id)
            if owner_ensemble_id is None:
                raise HTTPException(status_code=404, detail=f"{resource_type.capitalize()} not found")
            if owner_ensemble_id != str(person["ensemble_id"]):
                raise HTTPException(status_code=403, detail="Forbidden")
        if admin_only and person["user_role"] != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        return person
    return _dep


# --- Superadmin Bearer token (hardened) ---

_admin_scheme = HTTPBearer()
_TAKE_FIVE_ADMIN_API_KEY = os.getenv("TAKE_FIVE_ADMIN_API_KEY")

if not _TAKE_FIVE_ADMIN_API_KEY:
    logger.warning(
        "TAKE_FIVE_ADMIN_API_KEY not set — superadmin routes will reject all requests with 503."
    )


def verify_admin_token(credentials: HTTPAuthorizationCredentials = Security(_admin_scheme)) -> str:
    if not _TAKE_FIVE_ADMIN_API_KEY:
        # Fail closed: reject every request rather than running unsecured.
        # Scoped to secure_router only — /health, /leads, and the inbound
        # webhooks don't depend on this and stay up.
        raise HTTPException(status_code=503, detail="Admin API is not configured")
    if not hmac.compare_digest(credentials.credentials, _TAKE_FIVE_ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.credentials
