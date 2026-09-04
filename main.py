import asyncio
import logging
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, APIRouter, Request, Form, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from take_five.auth import (
    auth_router, verify_admin_token, get_current_person, require_admin,
    require_ensemble_scope, person_payload, ensemble_payload,
)
from take_five.integrations.groupme import handle_groupme_webhook, send_message_async, groupme_reply, get_groupme_user_id
from take_five.integrations.chat import setup_chat_circle, add_person_to_chat, remove_person_from_chat
from take_five.integrations.npi import search_npi
from take_five.integrations.twilio import handle_sms, send_sms
from take_five.integrations.sendgrid_email import handle_inbound_email
from take_five.messages import ask_with_tools, generate_prep_packet
from take_five.pipeline import run_post_storage_pipeline
from take_five.repository import repo
from take_five.schemas import (
    CreatePersonRequest, UpdatePersonRequest,
    CreateCareCircleRequest, UpdateCareCircleRequest,
    CreateCircleMembershipRequest,
    CreateEnsembleRequest, UpdateEnsembleRequest,
    CreateClinicalRecordRequest, UpdateClinicalRecordRequest,
    UpdateEnsembleMembershipRequest,
    InvitePersonRequest,
    CreateLeadRequest,
    MessageRequest, DigestRequest,
    CreateReferenceThreadRequest,
)
from take_five.summaries import generate_weekly_digest, generate_outer_weekly_digest
from take_five.utils import row_to_dict, row_list_to_dict_list

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://localhost:3000",
        "http://localhost:10000",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:10000",
        "http://0.0.0.0:10000",
        "https://take-five.onrender.com",
        "https://app.takefive.care",
        "https://app.takefivecare.com",
        "https://takefive.care",
        "https://www.takefive.care",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

open_router = APIRouter()
secure_router = APIRouter(dependencies=[Depends(verify_admin_token)])

@secure_router.post("/digest")
async def summary(body: DigestRequest):
    logger.info("Digest request received")
    kwargs = {}
    if body.start_date:
        kwargs['start_date'] = body.start_date
    if body.end_date:
        kwargs['end_date'] = body.end_date

    circle = repo.get_circle_by_id(body.circle_id)
    if circle and circle.get('parent_circle_id'):
        # Outer circle -- redacted digest, reads inner circle too. See
        # t5_week_summary_outer.md and summaries._contains_restricted_content.
        result = generate_outer_weekly_digest(body.circle_id, **kwargs)
        return {
            "digest": result["digest"],
            "blocked": result["blocked"],
            "flagged_terms": result["flagged_terms"],
        }

    digest = generate_weekly_digest(body.circle_id, **kwargs)
    return {"digest": digest}

@secure_router.post("/ensembles")
async def create_ensemble(body: CreateEnsembleRequest):
    logger.info(f"Create ensemble request received: {body.name}")
    ensemble = repo.create_ensemble(body.name, body.plan, body.status)
    return {"ensemble": row_to_dict(ensemble)}

@secure_router.get("/ensembles")
async def get_ensembles():
    logger.info("Get ensembles request received")
    ensembles = repo.list_ensembles()
    return {"ensembles": row_list_to_dict_list(ensembles)}

@secure_router.put("/ensembles/{ensemble_id}")
async def update_ensemble(ensemble_id: str, body: UpdateEnsembleRequest):
    """Patch ensemble name/plan/status. Superadmin only — archiving an
    ensemble is an offboarding action, not a family self-serve option."""
    if body.status is not None and body.status not in ('trial', 'active', 'archived'):
        raise HTTPException(status_code=400, detail="status must be 'trial', 'active', or 'archived'")
    ensemble = repo.update_ensemble(
        ensemble_id=ensemble_id,
        name=body.name,
        plan=body.plan,
        status=body.status,
    )
    if not ensemble:
        raise HTTPException(status_code=404, detail="Ensemble not found")
    return {"ensemble": row_to_dict(ensemble)}

@secure_router.get("/ensembles/{ensemble_id}/people")
async def get_ensemble_people(ensemble_id: str):
    logger.info(f"Get people for ensemble {ensemble_id}")
    people = repo.list_people_by_ensemble(ensemble_id)
    return {"people": [row_to_dict(row) for row in people]}

@secure_router.post("/ensembles/{ensemble_id}/people")
async def create_person(ensemble_id: str, body: CreatePersonRequest):
    person = repo.add_person_to_ensemble(
        ensemble_id=ensemble_id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases or [],
        external_id=body.external_id,
        notes=body.notes,
        date_of_birth=body.date_of_birth,
        clinical_access=body.clinical_access,
    )
    return {"person": row_to_dict(person)}

@secure_router.get("/people/{person_id}")
async def get_person(person_id: str):
    person = repo.get_person_by_id(person_id)
    return {"person": row_to_dict(person)}

@secure_router.put("/people/{person_id}")
async def update_person(person_id: str, body: UpdatePersonRequest):
    person = repo.update_person(
        person_id=person_id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases,
        notes=body.notes,
        external_id=body.external_id,
        date_of_birth=body.date_of_birth,
    )
    return {"person": row_to_dict(person)}


@secure_router.put("/people/{person_id}/membership")
async def update_person_membership(person_id: str, body: UpdateEnsembleMembershipRequest):
    """Set or update a person's user role in an ensemble."""
    if body.user_role not in ('admin', 'member'):
        raise HTTPException(status_code=400, detail="user_role must be 'admin' or 'member'")
    membership = repo.upsert_ensemble_membership(
        ensemble_id=body.ensemble_id,
        person_id=person_id,
        user_role=body.user_role,
    )
    return {"membership": row_to_dict(membership)}

@secure_router.get("/ensembles/{ensemble_id}/circles")
async def get_care_circles(ensemble_id: str):
    circles = repo.list_care_circles(ensemble_id=ensemble_id)
    return {"circles": [row_to_dict(row) for row in circles]}

@secure_router.post("/ensembles/{ensemble_id}/circles")
async def create_care_circle(ensemble_id: str, body: CreateCareCircleRequest):
    try:
        circle = repo.create_care_circle(
            ensemble_id=ensemble_id,
            name=body.name,
            status=body.status,
            external_id=body.external_id,
            parent_circle_id=body.parent_circle_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"circle": row_to_dict(circle)}

@secure_router.get("/circles/{circle_id}")
async def get_circle_by_id(circle_id: str):
    circle = repo.get_circle_by_id(circle_id)
    return {"people": row_to_dict(circle)}

@secure_router.put("/circles/{circle_id}")
async def update_care_circle(circle_id: str, body: UpdateCareCircleRequest):
    if body.status is not None and body.status not in ('active', 'archived'):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'archived'")
    circle = repo.update_care_circle(circle_id, body.model_dump(exclude_none=True))
    return {"circle": row_to_dict(circle)}

@secure_router.get("/circles/{circle_id}/people")
async def get_circle_people(circle_id: str):
    people = repo.fetch_circle_roster(circle_id)
    return {"people": [row_to_dict(row) for row in people]}

@secure_router.post("/circles/{circle_id}/groupme-setup")
async def groupme_setup(circle_id: str):
    """Create a chat group and bot for a care circle, add the ensemble admin.
    Routed through the chat.py dispatcher (GroupMe today; see take_five/integrations/chat.py)."""
    try:
        result = await setup_chat_circle(circle_id)
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@secure_router.post("/circles/{circle_id}/people/{person_id}/chat")
async def superadmin_add_person_to_chat(circle_id: str, person_id: str):
    """
    Add a circle member to the circle's chat platform (GroupMe today).
    Explicit, per-person action — distinct from circle membership itself;
    not every circle member should necessarily be in the chat (e.g. the
    senior, or a caregiver who'd rather just text a number). See Trello #59.
    """
    try:
        result = await add_person_to_chat(circle_id, person_id)
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@secure_router.delete("/circles/{circle_id}/people/{person_id}/chat")
async def superadmin_remove_person_from_chat(circle_id: str, person_id: str):
    """
    Remove a circle member from the circle's chat platform (GroupMe today).
    Does not remove them from the circle itself. Mirrors the add endpoint
    above. See Trello #59.
    """
    try:
        result = await remove_person_from_chat(circle_id, person_id)
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@secure_router.post("/circles/{circle_id}/people/{person_id}")
async def add_person_to_circle(circle_id: str, person_id: str,
                                body: CreateCircleMembershipRequest):
    membership = repo.add_person_to_circle(
        circle_id=circle_id,
        person_id=person_id,
        role=body.role
    )
    return {"membership": row_to_dict(membership)}

@secure_router.get("/npi/search")
async def npi_search(
    first_name: str = Query(...),
    last_name: str = Query(...),
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    enumeration_type: Optional[str] = Query(default="NPI-1", description="NPI-1 (individual), NPI-2 (organization), or omit for both"),
):
    results = await search_npi(first_name, last_name, city, state, enumeration_type)
    return {"results": results}


@secure_router.get("/circles/{circle_id}/clinical-records")
async def get_clinical_records(
    circle_id: str,
    person_id: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
):
    if person_id:
        records = repo.get_clinical_records(
            person_id=person_id,
            resource_type=resource_type,
        )
    else:
        records = repo.get_clinical_records_for_circle(
            circle_id=circle_id,
            resource_type=resource_type,
        )
    return {"records": [row_to_dict(r) for r in records]}


@secure_router.post("/circles/{circle_id}/clinical-records")
async def create_clinical_record(circle_id: str, body: CreateClinicalRecordRequest):
    record = repo.save_clinical_record(
        person_id=body.person_id,
        resource_type=body.resource_type,
        data=body.data,
        notes=body.notes,
        status=body.status,
        # circle_id intentionally omitted for admin entry
    )
    return {"record": row_to_dict(record)}


@secure_router.put("/clinical-records/{record_id}")
async def update_clinical_record(record_id: str, body: UpdateClinicalRecordRequest):
    record = repo.update_clinical_record(
        record_id=record_id,
        data=body.data,
        notes=body.notes,
        status=body.status,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"record": row_to_dict(record)}


@secure_router.get("/circles/{circle_id}/analytics")
async def get_circle_analytics(circle_id: str, days: Optional[int] = Query(None)):
    raw = repo.get_circle_analytics(circle_id, days=days)
    return {
        'weekly':   [row_to_dict(r) for r in raw['weekly']],
        'hourly':   [row_to_dict(r) for r in raw['hourly']],
        'members':  [row_to_dict(r) for r in raw['members']],
        'totals':   row_to_dict(raw['totals'])   if raw['totals']   else {},
        'clinical': row_to_dict(raw['clinical']) if raw['clinical'] else {'total': 0},
    }


@secure_router.get("/circles/{circle_id}/topics")
async def get_circle_topics(circle_id: str, days: Optional[int] = Query(None)):
    return repo.get_circle_topics(circle_id, days=days)


def _fire_reference_pipeline(entries: list, inserted: list, channel: str) -> None:
    """
    Reference content skipped run_post_storage_pipeline entirely when this
    feature first shipped — insert_reference_messages() only wrote the raw
    row. That meant no message_chunks (no embeddings, so semantic search
    over message history never finds this content once it ages out of the
    14-day recency window ask() also checks) and no clinical signal
    detection, even though reference content is exactly the kind of thing
    likely to carry real signals (a facility contact's note about UTIs, a
    doctor's treatment plan). Fire the same pipeline every other inbound
    channel (GroupMe, SMS) already gets, same fire-and-forget pattern as
    take_five.integrations.groupme's webhook handler.

    entries and inserted are the same length, same order (insert_reference_
    messages iterates entries in place and appends to inserted 1:1) — zip
    is safe here without a separate id-matching step.
    """
    for entry, row in zip(entries, inserted):
        if entry.get('person_id'):
            person = repo.get_person_by_id(entry['person_id'])
            sender = person['name'] if person else 'Unknown'
        else:
            sender = entry.get('external_name') or 'Unknown'
        asyncio.create_task(run_post_storage_pipeline(
            message_id=str(row['id']),
            circle_id=str(row['circle_id']),
            body=row['body'],
            sender=sender,
            sent_at=row['sent_at'],
            channel=channel,
        ))


@secure_router.get("/circles/{circle_id}/reference-threads")
async def get_circle_reference_threads(circle_id: str):
    """Thread labels already logged via the admin References tab, for the
    'already logged for this thread' panel shown before adding more."""
    threads = repo.get_reference_threads(circle_id)
    return {"threads": row_list_to_dict_list(threads)}


@secure_router.get("/circles/{circle_id}/reference-threads/{thread_label}")
async def get_circle_reference_thread_messages(circle_id: str, thread_label: str):
    messages = repo.get_reference_messages(circle_id, thread_label)
    return {"messages": row_list_to_dict_list(messages)}


@secure_router.post("/circles/{circle_id}/reference-threads")
async def create_circle_reference_thread(circle_id: str, body: CreateReferenceThreadRequest):
    """
    Admin-entered external reference content (email threads, documents) that
    predates ingestion — see /admin References tab. Each entry becomes its
    own messages row with message_type='external_reference', keeping it out
    of the weekly digest while remaining fully retrievable via ask() and
    decision support.
    """
    entries = [
        {
            'person_id': m.person_id,
            'external_name': m.external_name,
            'external_org': m.external_org,
            'sent_at': m.sent_at,
            'body': m.body,
        }
        for m in body.messages
    ]
    inserted = repo.insert_reference_messages(
        circle_id=circle_id,
        channel=body.channel,
        thread_label=body.thread_label,
        entries=entries,
    )
    _fire_reference_pipeline(entries, inserted, body.channel)
    return {"messages": row_list_to_dict_list(inserted)}


@secure_router.post("/messages")
async def message(body: MessageRequest):
    logger.info("Message received")
    response = await ask_with_tools(
        question=body.message,
        circle_id=body.circle_id,
        response_format=body.response_format,
        confirmed_by_person_id=None,  # no person context via API
    )
    return {"response": response}

# --- Open endpoints for external integrations ---

@open_router.post("/groupme/webhook")
async def groupme_webhook(request: Request):
    data = await request.json()
    return await handle_groupme_webhook(data)


@open_router.get("/groupme/oauth/callback")
async def groupme_oauth_callback():
    """
    Landing page for the GroupMe OAuth implicit-grant redirect. Opened in a
    popup window (not an iframe — GroupMe's login page won't allow framing,
    same as virtually every OAuth provider), never navigated to directly.

    GroupMe's own docs describe the token arriving in the URL *fragment*
    (#access_token=...), which never reaches a server automatically —
    that's why this needs a client-side script at all rather than a plain
    server-side query param read. In practice (confirmed live, 2026-08-03),
    GroupMe actually delivers it as a *query string* param instead
    (?access_token=...), contradicting the docs. The script below checks
    both, so it works regardless of which one GroupMe actually does on any
    given day. The script reads whichever is present, posts the token back
    to the window that opened the popup via postMessage, then closes
    itself. The opener (wherever the "Connect GroupMe" button lives)
    listens for that message and POSTs the token to
    /app/groupme/oauth/token to actually store it. See Trello #39.
    """
    html = """<!DOCTYPE html>
<html>
<head><title>Connecting GroupMe…</title></head>
<body style="font-family: sans-serif; text-align: center; padding-top: 3rem; color: #085041;">
<p id="status">Connecting your GroupMe account…</p>
<div id="manual" style="display:none; margin-top:1.5rem; font-size:0.85rem; color:#6B6763;">
  <p>Automatic connection didn't complete (this can happen in Safari). Copy this code and paste it into the "Connect GroupMe" box in Take Five:</p>
  <input id="tokenBox" readonly style="width:80%;max-width:400px;padding:0.5rem;margin-top:0.5rem;font-family:monospace;" onclick="this.select()">
</div>
<script>
  (function () {
    var statusEl = document.getElementById('status');
    function showManualFallback(token, reason) {
      statusEl.textContent = 'Connected — finish this step manually:';
      document.getElementById('manual').style.display = 'block';
      document.getElementById('tokenBox').value = token;
      console.warn('[takefive-groupme-oauth] Falling back to manual token entry:', reason);
    }
    try {
      var hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      var queryParams = new URLSearchParams(window.location.search);
      var token = hashParams.get('access_token') || queryParams.get('access_token');
      if (!token) {
        statusEl.textContent = 'Something went wrong — no token received. You can close this window and try again.';
        return;
      }
      var opener;
      try {
        opener = window.opener;
      } catch (e) {
        showManualFallback(token, 'window.opener threw: ' + e.message);
        return;
      }
      if (!opener) {
        showManualFallback(token, 'window.opener is null/undefined');
        return;
      }
      try {
        opener.postMessage({ source: 'takefive-groupme-oauth', access_token: token }, '*');
        statusEl.textContent = 'Connected! This window will close automatically.';
        setTimeout(function () { window.close(); }, 800);
      } catch (e) {
        showManualFallback(token, 'postMessage threw: ' + e.message);
      }
    } catch (e) {
      statusEl.textContent = 'Something went wrong (' + e.message + '). You can close this window and try again.';
      console.error('[takefive-groupme-oauth]', e);
    }
  })();
</script>
</body>
</html>"""
    return Response(
        content=html,
        media_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@open_router.post("/sendgrid/inbound")
async def sendgrid_inbound(request: Request):
    """
    SendGrid Inbound Parse webhook. One hostname (combo.takefive.care) is
    configured in SendGrid to POST every address's mail here -- routing to
    the right circle happens inside handle_inbound_email() by decoding the
    recipient local-part back into a circle_id, not via separate
    per-circle SendGrid config. See take_five/integrations/sendgrid_email.py.
    """
    return await handle_inbound_email(request)


@open_router.post("/twilio/sms")
async def receive_sms(
    From: str = Form(...),
    Body: str = Form(...),
    To: str = Form(...),
    NumMedia: str = Form(default="0"),
    MediaUrl0: Optional[str] = Form(default=None),
    MediaContentType0: Optional[str] = Form(default=None),
):
    return await handle_sms(
        From=From,
        Body=Body,
        To=To,
        NumMedia=NumMedia,
        MediaUrl0=MediaUrl0,
        MediaContentType0=MediaContentType0,
    )


app.include_router(secure_router)
app.include_router(auth_router)

@open_router.get("/health")
async def health():
    logger.info("Health check requested")
    return {"status": "ok"}


# --- User-facing endpoints (ensemble admin / member pages) ---
# Auth is a Bearer session token issued by /auth/otp/verify (see take_five/auth.py).

@open_router.get("/app/me")
async def app_me(person: dict = Depends(get_current_person)):
    """Validate/restore a stored session on page load."""
    # Deliberately checks person_channel_credentials (a live, usable OAuth
    # token), not person_channel_identities. A person can have a known
    # GroupMe identity (e.g. backfilled from the old flat external_id
    # column, card #63) without ever having gone through the OAuth flow —
    # checking identities here would show "Connected" for people who
    # haven't actually authorized anything, which is exactly the false
    # positive found testing this on 2026-08-03.
    groupme_credential = repo.get_person_channel_credential(str(person["person_id"]), "groupme")
    groupme_connected = groupme_credential is not None
    return {
        "person": person_payload(person),
        "ensemble": ensemble_payload(person),
        "user_role": person["user_role"],
        "groupme_connected": groupme_connected,
    }


@open_router.post("/app/groupme/oauth/token")
async def app_groupme_oauth_token(
    body: dict = Body(...),
    person: dict = Depends(require_admin),
):
    """
    Exchange a GroupMe access token (captured client-side from the OAuth
    popup's callback page — see /groupme/oauth/callback above) for stored
    identity + credential rows on the calling admin's own person record.
    Admin-only; always writes against the authenticated caller, never a
    person_id from the request body — an admin can only connect their own
    GroupMe account, not someone else's.

    Calls GroupMe's GET /users/me with the token to resolve the account's
    real GroupMe user_id (so the same account is recognized consistently
    even if the token itself is later replaced — tokens don't carry
    identity on their own, per Trello #39's confirmed API behavior).

    Idempotent: repo.upsert_person_channel_identity is a safe no-op if this
    exact (channel, external_id) pair is already linked to this person, and
    repo.upsert_person_channel_credential replaces any prior stored token
    for this person/channel outright — confirmed safe, since a new token
    for the same GroupMe account carries identical permissions to the old
    one (see card #39 design notes).
    """
    access_token = body.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    person_id = str(person["person_id"])
    try:
        groupme_user_id = await get_groupme_user_id(access_token)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=f"Could not verify GroupMe token: {e}")

    try:
        repo.upsert_person_channel_identity(person_id, "groupme", groupme_user_id)
    except ValueError as e:
        # This GroupMe account is already linked to a different person —
        # a real conflict, surfaced rather than silently overwritten (see
        # repo.upsert_person_channel_identity's docstring).
        raise HTTPException(status_code=409, detail=str(e))

    repo.upsert_person_channel_credential(person_id, "groupme", access_token)

    logger.info(f"[groupme-oauth] Connected GroupMe account {groupme_user_id} to person {person_id}")
    return {"status": "connected", "groupme_user_id": groupme_user_id}


@open_router.post("/app/circles/{circle_id}/groupme-setup")
async def app_groupme_setup(
    circle_id: str,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")], admin_only=True)),
):
    """
    Create a chat group and bot for a care circle. Admin-only.
    Routed through the chat.py dispatcher (GroupMe today; see take_five/integrations/chat.py).

    Passes the calling admin's person_id through so the GroupMe group gets
    created under *their* account (via their stored OAuth token, if they've
    connected one — see /app/groupme/oauth/token below) rather than the
    shared GROUPME_USER_ACCESS_TOKEN. See Trello #39. Falls back to the env
    var automatically if this admin hasn't gone through the OAuth flow yet
    (resolve_groupme_token's fallback), so this is safe to ship before every
    admin has connected an account.
    """
    try:
        result = await setup_chat_circle(circle_id, admin_person_id=str(person["person_id"]))
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@open_router.post("/app/circles/{circle_id}/people/{person_id}/chat")
async def app_add_person_to_chat(
    circle_id: str,
    person_id: str,
    person: dict = Depends(require_ensemble_scope(
        [("circle", "circle_id"), ("person", "person_id")], admin_only=True,
    )),
):
    """
    Add a circle member to the circle's chat platform (GroupMe today).
    Admin-only. Explicit, per-person action — distinct from circle
    membership itself; not every circle member should necessarily be in the
    chat (e.g. the senior, or a caregiver who'd rather just text a number).
    See Trello #59.
    """
    try:
        result = await add_person_to_chat(circle_id, person_id)
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@open_router.delete("/app/circles/{circle_id}/people/{person_id}/chat")
async def app_remove_person_from_chat(
    circle_id: str,
    person_id: str,
    person: dict = Depends(require_ensemble_scope(
        [("circle", "circle_id"), ("person", "person_id")], admin_only=True,
    )),
):
    """
    Remove a circle member from the circle's chat platform (GroupMe today).
    Admin-only. Does not remove them from the circle itself — that's a
    separate action (DELETE .../members/{person_id}). Mirrors the add
    endpoint above. See Trello #59.
    """
    try:
        result = await remove_person_from_chat(circle_id, person_id)
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@open_router.post("/app/ensembles/{ensemble_id}/circles")
async def app_create_circle(
    ensemble_id: str,
    body: CreateCareCircleRequest,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")], admin_only=True)),
):
    """
    Create a care circle in the ensemble. Admin-only.
    """
    try:
        circle = repo.create_care_circle(
            ensemble_id=ensemble_id,
            name=body.name,
            status=body.status or "active",
            external_id=body.external_id,
            parent_circle_id=body.parent_circle_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"circle": row_to_dict(circle)}


@open_router.get("/app/ensembles/{ensemble_id}/circles")
async def app_get_circles(
    ensemble_id: str,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")])),
):
    """
    Return circles visible to the requester.
    Admins see all circles; members see only their own.
    """
    circles = repo.list_circles_for_person(
        ensemble_id=ensemble_id,
        person_id=str(person["person_id"]),
        user_role=person["user_role"],
    )
    return {"circles": [row_to_dict(c) for c in (circles or [])]}


@open_router.get("/app/ensembles/{ensemble_id}/people")
async def app_get_people(
    ensemble_id: str,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")])),
):
    """
    Return people visible to the requester.
    Admins see all; members see only people in their circles.
    """
    people = repo.list_people_for_person(
        ensemble_id=ensemble_id,
        person_id=str(person["person_id"]),
        user_role=person["user_role"],
    )
    return {"people": [row_to_dict(p) for p in (people or [])]}


@open_router.get("/app/ensembles/{ensemble_id}/activity")
async def app_get_activity(
    ensemble_id: str,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")])),
):
    """
    Return recent messages and last digest visible to the requester.
    """
    messages = repo.get_ensemble_activity(
        ensemble_id=ensemble_id,
        person_id=str(person["person_id"]),
        user_role=person["user_role"],
    )
    last_digests = repo.get_last_digest(
        ensemble_id,
        person_id=str(person["person_id"]),
        user_role=person["user_role"],
    )
    return {
        "messages": [row_to_dict(m) for m in (messages or [])],
        "last_digests": [row_to_dict(d) for d in (last_digests or [])],
    }

@open_router.get("/app/ensembles/{ensemble_id}/medications")
async def app_get_medications(
    ensemble_id: str,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")])),
):
    """
    Return active medications for all seniors in the ensemble.

    Gated on person_has_clinical_access(), independent of ensemble/circle
    membership — require_ensemble_scope alone only confirms this person
    belongs to the ensemble, not that they're cleared to see clinical data
    (an outer-circle-only member should not get this by virtue of ensemble
    membership). See card #44.
    """
    if not repo.person_has_clinical_access(str(person["person_id"])):
        raise HTTPException(status_code=403, detail="Not authorized to view clinical records")
    meds = repo.get_medications_for_ensemble(ensemble_id)
    return {"medications": [row_to_dict(m) for m in (meds or [])]}


@open_router.put("/app/people/me")
async def app_update_me(
    body: UpdatePersonRequest,
    person: dict = Depends(get_current_person),
):
    """
    Allow a person to update their own profile fields.
    Scoped to phone, aliases, notes, date_of_birth only — no name or type changes.
    """
    updated = repo.update_person(
        person_id=str(person["person_id"]),
        phone=body.phone,
        aliases=body.aliases,
        notes=body.notes,
        date_of_birth=body.date_of_birth,
    )
    return {"person": row_to_dict(updated)}


@open_router.put("/app/people/{person_id}")
async def app_update_person(
    person_id: str,
    body: UpdatePersonRequest,
    person: dict = Depends(require_ensemble_scope([("person", "person_id")], admin_only=True)),
):
    """
    Update a person's profile. Admin-only.

    clinical_access is intentionally included here (admin-only path) but not
    on /app/people/me below — people should not be able to grant themselves
    clinical access. See card #44.
    """
    updated = repo.update_person(
        person_id=person_id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases,
        notes=body.notes,
        clinical_access=body.clinical_access,
    )
    return {"person": row_to_dict(updated)}


@open_router.put("/app/circles/{circle_id}/people/{person_id}/role")
async def app_update_circle_role(
    circle_id: str,
    person_id: str,
    body: CreateCircleMembershipRequest,
    person: dict = Depends(require_ensemble_scope(
        [("circle", "circle_id"), ("person", "person_id")], admin_only=True,
    )),
):
    """
    Update a person's role in a specific circle. Admin-only.
    """
    if body.role not in ('senior', 'family', 'friend', 'caregiver'):
        raise HTTPException(status_code=400, detail="Invalid role")
    membership = repo.add_person_to_circle(
        circle_id=circle_id,
        person_id=person_id,
        role=body.role,
    )
    return {"membership": row_to_dict(membership)}


@open_router.put("/app/people/{person_id}/membership")
async def app_update_person_membership(
    person_id: str,
    body: UpdateEnsembleMembershipRequest,
    person: dict = Depends(require_ensemble_scope([("person", "person_id")], admin_only=True)),
):
    """
    Update a person's user role in an ensemble. Admin-only.
    """
    if body.ensemble_id != str(person["ensemble_id"]):
        raise HTTPException(status_code=403, detail="Forbidden")
    if body.user_role not in ('admin', 'member'):
        raise HTTPException(status_code=400, detail="user_role must be 'admin' or 'member'")
    membership = repo.upsert_ensemble_membership(
        ensemble_id=body.ensemble_id,
        person_id=person_id,
        user_role=body.user_role,
    )
    return {"membership": row_to_dict(membership)}


@open_router.get("/app/ensembles/{ensemble_id}/digests")
async def app_get_digests(
    ensemble_id: str,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")])),
):
    """
    Return digest history for the ensemble.
    Circle-scoped for non-admins — see get_digest_history()'s docstring.
    """
    digests = repo.get_digest_history(
        ensemble_id,
        person_id=str(person["person_id"]),
        user_role=person["user_role"],
    )
    return {"digests": [row_to_dict(d) for d in (digests or [])]}


@open_router.post("/app/ensembles/{ensemble_id}/invite")
async def app_invite_person(
    ensemble_id: str,
    body: InvitePersonRequest,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")], admin_only=True)),
):
    """
    Create (or update) a person in the ensemble and add them to a circle.
    Admin-only. Idempotent — safe to call again if the person already exists.
    Returns the person row and the invite URL.
    """
    if body.user_role not in ('admin', 'member'):
        raise HTTPException(status_code=400, detail="user_role must be 'admin' or 'member'")
    if body.care_role not in ('senior', 'family', 'friend', 'caregiver'):
        raise HTTPException(status_code=400, detail="Invalid care_role")

    invited = repo.invite_person_to_ensemble(
        ensemble_id=ensemble_id,
        circle_id=body.circle_id,
        name=body.name,
        email=body.email,
        phone=body.phone,
        care_role=body.care_role,
        user_role=body.user_role,
        clinical_access=body.clinical_access,
    )
    invite_url = "https://app.takefive.care"
    return {"person": row_to_dict(invited), "invite_url": invite_url}


@open_router.get("/app/ensembles/{ensemble_id}/clinical-records")
async def app_get_clinical_records(
    ensemble_id: str,
    resource_type: Optional[str] = Query(None),
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")])),
):
    """
    Return clinical records (medications, care team) for all seniors
    in the ensemble.

    Gated on person_has_clinical_access(), independent of ensemble/circle
    membership — "readable by all members" (previous docstring) was the bug:
    require_ensemble_scope alone only confirms ensemble membership, not
    clinical clearance, so an outer-circle-only member would otherwise see
    every senior's full medications/care team. Writes remain admin-only
    (unchanged, separate endpoint below). See card #44.
    """
    if not repo.person_has_clinical_access(str(person["person_id"])):
        raise HTTPException(status_code=403, detail="Not authorized to view clinical records")
    records = repo.get_clinical_records_for_ensemble(
        ensemble_id=ensemble_id,
        resource_type=resource_type,
    )
    return {"records": [row_to_dict(r) for r in (records or [])]}


@open_router.post("/app/ensembles/{ensemble_id}/clinical-records")
async def app_create_clinical_record(
    ensemble_id: str,
    body: CreateClinicalRecordRequest,
    person: dict = Depends(require_ensemble_scope([("ensemble", "ensemble_id")], admin_only=True)),
):
    """
    Create a clinical record (medication or care team member) for a senior
    in the ensemble. Admin-only.
    """
    record = repo.save_clinical_record(
        person_id=body.person_id,
        resource_type=body.resource_type,
        data=body.data,
        notes=body.notes,
        status=body.status,
    )
    return {"record": row_to_dict(record)}


@open_router.put("/app/clinical-records/{record_id}")
async def app_update_clinical_record(
    record_id: str,
    body: UpdateClinicalRecordRequest,
    person: dict = Depends(require_ensemble_scope([("clinical_record", "record_id")], admin_only=True)),
):
    """
    Update a clinical record. Admin-only, and scoped to the record's own
    ensemble (via require_ensemble_scope's clinical_record resolver) rather
    than trusted on the strength of the UUID alone.
    """
    record = repo.update_clinical_record(
        record_id=record_id,
        data=body.data,
        notes=body.notes,
        status=body.status,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"record": row_to_dict(record)}


@open_router.get("/app/circles/{circle_id}/roster")
async def app_get_circle_roster(
    circle_id: str,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")])),
):
    """
    Return the roster for a specific circle.
    Visible to all members in the circle; admins can see any circle.
    """
    roster = repo.fetch_circle_roster(circle_id)
    return {"roster": [row_to_dict(r) for r in (roster or [])]}


@open_router.post("/app/circles/{circle_id}/members")
async def app_add_circle_member(
    circle_id: str,
    body: CreateCircleMembershipRequest,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")], admin_only=True)),
):
    """
    Add an existing ensemble person to a circle with a role. Admin-only.
    person_id must be supplied in the request body.
    """
    if body.role not in ('senior', 'family', 'friend', 'caregiver'):
        raise HTTPException(status_code=400, detail="Invalid role")
    membership = repo.add_person_to_circle(
        circle_id=circle_id,
        person_id=body.person_id,
        role=body.role,
    )
    return {"membership": row_to_dict(membership)}


@open_router.delete("/app/circles/{circle_id}/members/{person_id}")
async def app_remove_circle_member(
    circle_id: str,
    person_id: str,
    person: dict = Depends(require_ensemble_scope(
        [("circle", "circle_id"), ("person", "person_id")], admin_only=True,
    )),
):
    """
    Remove a person from a circle. Admin-only.
    Does not delete the person from the ensemble.
    """
    repo.remove_person_from_circle(circle_id=circle_id, person_id=person_id)
    return {"removed": True}


@open_router.get("/app/npi/search")
async def app_npi_search(
    first_name: str = Query(...),
    last_name: str = Query(...),
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    person: dict = Depends(get_current_person),
):
    """
    NPI registry search proxy for the ensemble admin page.
    Any authenticated member can look up providers (no admin check).
    """
    results = await search_npi(first_name, last_name, city, state, enumeration_type="NPI-1")
    return {"results": results}


@open_router.get("/app/circles/{circle_id}/prep-packets")
async def app_get_prep_packets(
    circle_id: str,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")])),
):
    """
    Return previously generated prep packets for a circle.
    """
    packets = repo.get_prep_packets(circle_id)
    return {"packets": [row_to_dict(p) for p in (packets or [])]}


@open_router.post("/app/circles/{circle_id}/prep-packet")
async def app_generate_prep_packet(
    circle_id: str,
    body: dict = Body(...),
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")])),
):
    """
    Generate an appointment prep packet and post it to GroupMe.
    Accessible to all circle members (not admin-only).
    """
    doctor_name       = body.get("doctor_name", "the doctor")
    appointment_desc  = body.get("appointment_desc", "upcoming appointment")
    # The admin frontend's prep-packet modal already has a patient selector
    # (auto-hidden for single-senior circles) and sends the chosen senior as
    # person_id — it was just being silently ignored by this endpoint before.
    senior_person_id  = body.get("person_id") or body.get("senior_person_id")

    # If the circle only has one senior, resolve it automatically so older
    # frontend builds (or callers that don't send person_id) still work.
    if not senior_person_id:
        roster = repo.fetch_circle_roster(circle_id)
        seniors = [r for r in roster if r.get("person_role") == "senior"]
        if len(seniors) == 1:
            senior_person_id = str(seniors[0]["id"])

    # Build a fallback question string in case generate_prep_packet ever needs
    # to fall back to free-text parsing (it won't here, since doctor_name and
    # appointment_desc are passed in directly and Step 1's parse is skipped).
    question = f"prep for appointment with {doctor_name} {appointment_desc}"

    try:
        packet_text, followup_text = await generate_prep_packet(
            question=question,
            circle_id=circle_id,
            sender_person_id=str(person["person_id"]),
            doctor_name=doctor_name,
            appointment_desc=appointment_desc,
            senior_person_id=senior_person_id,
        )

        # Post to GroupMe if the circle has a bot configured
        circle = repo.get_circle_by_id(circle_id)
        bot_id = (circle.get("integration_config") or {}).get("groupme_bot_id") if circle else None
        circle_ext_id = circle.get("external_id") if circle else None

        if bot_id and circle_ext_id:
            import asyncio
            # Post the packet directly — it's already logged as message_type='prep_packet'
            # inside generate_prep_packet(), so don't double-log via groupme_reply()
            await send_message_async(bot_id, packet_text)
            await asyncio.sleep(1.5)
            # Follow-up prompt is fine to log normally as an agent_note
            await groupme_reply(bot_id, followup_text, circle_ext_id)
        else:
            logger.warning(f"[prep-packet] No GroupMe bot configured for circle {circle_id}")

        return {"packet": packet_text, "followup": followup_text}

    except Exception as e:
        logger.error(f"[prep-packet] Failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@open_router.get("/app/circles/{circle_id}/reference-threads")
async def app_get_circle_reference_threads(
    circle_id: str,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")], admin_only=True)),
):
    """Thread labels already logged for this circle via the References
    section. Admin-only — same gate as adding members or medications."""
    threads = repo.get_reference_threads(circle_id)
    return {"threads": row_list_to_dict_list(threads)}


@open_router.get("/app/circles/{circle_id}/reference-threads/{thread_label}")
async def app_get_circle_reference_thread_messages(
    circle_id: str,
    thread_label: str,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")], admin_only=True)),
):
    messages = repo.get_reference_messages(circle_id, thread_label)
    return {"messages": row_list_to_dict_list(messages)}


@open_router.post("/app/circles/{circle_id}/reference-threads")
async def app_create_circle_reference_thread(
    circle_id: str,
    body: CreateReferenceThreadRequest,
    person: dict = Depends(require_ensemble_scope([("circle", "circle_id")], admin_only=True)),
):
    """
    Admin-entered external reference content (email threads, documents) that
    predates ingestion — ensemble-admin counterpart to the superadmin route
    of the same shape. Each entry becomes its own messages row with
    message_type='external_reference', keeping it out of the weekly digest
    while remaining fully retrievable via ask() and decision support.
    """
    entries = [
        {
            'person_id': m.person_id,
            'external_name': m.external_name,
            'external_org': m.external_org,
            'sent_at': m.sent_at,
            'body': m.body,
        }
        for m in body.messages
    ]
    inserted = repo.insert_reference_messages(
        circle_id=circle_id,
        channel=body.channel,
        thread_label=body.thread_label,
        entries=entries,
    )
    _fire_reference_pipeline(entries, inserted, body.channel)
    return {"messages": row_list_to_dict_list(inserted)}


async def _send_sms_invite(person_id: str, circle_id: str) -> dict:
    """
    Send an SMS invite to a person on behalf of a specific circle, so they
    have the Take Five number saved. Shared by the open (session-authenticated
    family admin) and secure (Bearer-token superadmin) sms-invite routes —
    same send logic, different auth in front of it.
    """
    person = repo.get_person_by_id(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    if not person.get("phone"):
        raise HTTPException(status_code=400, detail="Person has no phone number")

    circle = repo.get_circle_by_id(circle_id)
    if not circle:
        raise HTTPException(status_code=404, detail="Circle not found")

    circle_name = circle["name"]

    # Find the senior(s)' name(s) for a personalised message — a circle can
    # have more than one (e.g. a couple), so name all of them, not just the
    # first.
    seniors = repo.get_seniors_in_circle(circle_id)
    if seniors:
        first_names = [s["name"].split()[0] for s in seniors]
        if len(first_names) == 1:
            senior_name = first_names[0]
        elif len(first_names) == 2:
            senior_name = f"{first_names[0]} and {first_names[1]}"
        else:
            senior_name = f"{', '.join(first_names[:-1])}, and {first_names[-1]}"
    else:
        senior_name = "your loved one"

    invite_body = (
        f"Hi {person['name'].split()[0]} - this is Take Five for {circle_name}. "
        f"Text this number after visits with {senior_name} and we'll keep the family in the loop."
    )

    try:
        send_sms(person["phone"], invite_body)
    except Exception as e:
        logger.error(f"[sms-invite] Failed to send to {person['name']}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")

    # Log to messages as agent_note so it appears in activity
    log_body = f"SMS invite sent to {person['name']} ({person['phone']})"
    repo.log_message(
        circle_ext_id=circle["external_id"],
        person_ext_id=None,
        body=log_body,
        msg_type="agent_note",
        direction="outbound",
        raw_data={"type": "sms_invite", "to_person_id": person_id, "to_phone": person["phone"]},
        channel="sms",
    )

    logger.info(f"[sms-invite] Sent to {person['name']} ({person['phone']}) for circle {circle_name}")
    return {"sent": True, "to": person["phone"], "person": person["name"]}


@secure_router.post("/people/{person_id}/sms-invite")
async def superadmin_sms_invite(person_id: str, circle_id: str = Query(...)):
    """
    Send an SMS invite (superadmin panel, Bearer-token auth). circle_id is
    required here since the superadmin UI always has an explicit circle
    selected — no need to infer one.
    """
    return await _send_sms_invite(person_id, circle_id)


@open_router.post("/app/people/{person_id}/sms-invite")
async def app_sms_invite(
    person_id: str,
    circle_id: Optional[str] = Query(None),
    person: dict = Depends(require_ensemble_scope([("person", "person_id")], admin_only=True)),
):
    """
    Send an SMS invite to a person so they have the Take Five number saved.
    Admin-only (session-authenticated family admin panel).
    """
    if not circle_id:
        # Fallback for callers that don't pass circle_id explicitly —
        # use the person's (first) circle.
        circles = repo.list_circles_for_person(
            ensemble_id=str(person["ensemble_id"]),
            person_id=person_id,
            user_role="admin",
        )
        first_circle = (circles or [None])[0]
        if not first_circle:
            raise HTTPException(status_code=400, detail="Person has no care circle")
        circle_id = str(first_circle["id"])

    return await _send_sms_invite(person_id, circle_id)


@open_router.get("/")
async def serve_app():
    return FileResponse('admin/takefive-ensemble-admin.html')

@open_router.get("/admin/{file_name}")
async def read_admin(file_name: str):
    return FileResponse(f'admin/{file_name}')

@open_router.post("/leads")
async def create_lead(body: CreateLeadRequest):
    """
    Public endpoint for the homepage pilot signup form (family or agency).
    No auth — this is a public lead-capture form. Honeypot field ('website')
    must stay empty; if filled, silently drop the submission.
    """
    if body.website:
        logger.info("[leads] Honeypot triggered, dropping submission")
        return {"status": "ok"}
    if body.lead_type not in ('family', 'agency'):
        raise HTTPException(status_code=400, detail="lead_type must be 'family' or 'agency'")
    lead = repo.create_lead(
        lead_type=body.lead_type,
        name=body.name,
        email=body.email,
        phone=body.phone,
        details=body.details,
        source=body.source or 'homepage',
    )
    logger.info(f"[leads] New {body.lead_type} lead: {body.name} ({body.email})")
    return {"status": "ok", "lead": row_to_dict(lead)}


app.include_router(open_router)
