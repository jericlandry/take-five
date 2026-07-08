import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Security, HTTPException, Depends, APIRouter, Request, Form, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from take_five.integrations.groupme import handle_groupme_webhook, send_message_async, groupme_reply, setup_groupme_circle
from take_five.integrations.npi import search_npi
from take_five.integrations.twilio import handle_sms
from take_five.messages import ask_with_tools, generate_prep_packet
from take_five.repository import repo
from take_five.schemas import (
    CreatePersonRequest, UpdatePersonRequest,
    CreateCareCircleRequest, UpdateCareCircleRequest,
    CreateCircleMembershipRequest,
    CreateEnsembleRequest,
    CreateClinicalRecordRequest, UpdateClinicalRecordRequest,
    UpdateEnsembleMembershipRequest,
    InvitePersonRequest,
    CreateLeadRequest,
    MessageRequest, DigestRequest,
)
from take_five.summaries import generate_weekly_digest
from take_five.utils import row_to_dict, row_list_to_dict_list

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

security = HTTPBearer()

TAKE_FIVE_ADMIN_API_KEY = os.getenv("TAKE_FIVE_ADMIN_API_KEY")
if not TAKE_FIVE_ADMIN_API_KEY:
    logger.warning("TAKE_FIVE_ADMIN_API_KEY not set in environment variables. Admin endpoints will be unsecured.")

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != TAKE_FIVE_ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.credentials

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
secure_router = APIRouter(dependencies=[Depends(verify_token)])

@secure_router.post("/digest")
async def summary(body: DigestRequest):
    logger.info("Digest request received")
    kwargs = {}
    if body.start_date:
        kwargs['start_date'] = body.start_date
    if body.end_date:
        kwargs['end_date'] = body.end_date
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
    circle = repo.create_care_circle(
        ensemble_id=ensemble_id,
        name=body.name,
        status=body.status,
        external_id=body.external_id,
    )
    return {"circle": row_to_dict(circle)}

@secure_router.get("/circles/{circle_id}")
async def get_circle_by_id(circle_id: str):
    circle = repo.get_circle_by_id(circle_id)
    return {"people": row_to_dict(circle)}

@secure_router.put("/circles/{circle_id}")
async def update_care_circle(circle_id: str, body: UpdateCareCircleRequest):
    circle = repo.update_care_circle(circle_id, body.model_dump(exclude_none=True))
    return {"circle": row_to_dict(circle)}

@secure_router.get("/circles/{circle_id}/people")
async def get_circle_people(circle_id: str):
    people = repo.fetch_circle_roster(circle_id)
    return {"people": [row_to_dict(row) for row in people]}

@secure_router.post("/circles/{circle_id}/groupme-setup")
async def groupme_setup(circle_id: str):
    """Create a GroupMe group and bot for a care circle, add the ensemble admin."""
    try:
        result = await setup_groupme_circle(circle_id)
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

@open_router.get("/health")
async def health():
    logger.info("Health check requested")
    return {"status": "ok"}


# --- User-facing endpoints (ensemble admin / member pages) ---
# Auth is the email lookup itself — acceptable for pilot scale.

@open_router.get("/auth/lookup")
async def auth_lookup(email: str = Query(...)):
    """
    Look up a person by email and return their session context.
    Used by the ensemble admin/member page on load.
    Returns 404 if the email is not found or has no ensemble_memberships row.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=404, detail="No account found for that email address")
    return {
        "person": {
            "id":             str(row["person_id"]),
            "name":           row["person_name"],
            "email":          row["email"],
            "phone":          row["phone"],
            "aliases":        row["aliases"] or [],
            "notes":          row["notes"],
            "date_of_birth":  str(row["date_of_birth"]) if row["date_of_birth"] else None,
        },
        "ensemble": {
            "id":     str(row["ensemble_id"]),
            "name":   row["ensemble_name"],
            "plan":   row["ensemble_plan"],
            "status": row["ensemble_status"],
        },
        "user_role": row["user_role"],
    }


@open_router.post("/app/circles/{circle_id}/groupme-setup")
async def app_groupme_setup(
    circle_id: str,
    email: str = Query(...),
):
    """
    Create a GroupMe group and bot for a care circle. Admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        result = await setup_groupme_circle(circle_id)
        return {"status": "ok", "result": result}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@open_router.post("/app/ensembles/{ensemble_id}/circles")
async def app_create_circle(
    ensemble_id: str,
    email: str = Query(...),
    body: CreateCareCircleRequest = ...,
):
    """
    Create a care circle in the ensemble. Admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    circle = repo.create_care_circle(
        ensemble_id=ensemble_id,
        name=body.name,
        status=body.status or "active",
        external_id=body.external_id,
    )
    return {"circle": row_to_dict(circle)}


@open_router.get("/app/ensembles/{ensemble_id}/circles")
async def app_get_circles(
    ensemble_id: str,
    email: str = Query(...),
):
    """
    Return circles visible to the requester.
    Admins see all circles; members see only their own.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    circles = repo.list_circles_for_person(
        ensemble_id=ensemble_id,
        person_id=str(row["person_id"]),
        user_role=row["user_role"],
    )
    return {"circles": [row_to_dict(c) for c in (circles or [])]}


@open_router.get("/app/ensembles/{ensemble_id}/people")
async def app_get_people(
    ensemble_id: str,
    email: str = Query(...),
):
    """
    Return people visible to the requester.
    Admins see all; members see only people in their circles.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    people = repo.list_people_for_person(
        ensemble_id=ensemble_id,
        person_id=str(row["person_id"]),
        user_role=row["user_role"],
    )
    return {"people": [row_to_dict(p) for p in (people or [])]}


@open_router.get("/app/ensembles/{ensemble_id}/activity")
async def app_get_activity(
    ensemble_id: str,
    email: str = Query(...),
):
    """
    Return recent messages and last digest visible to the requester.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    messages = repo.get_ensemble_activity(
        ensemble_id=ensemble_id,
        person_id=str(row["person_id"]),
        user_role=row["user_role"],
    )
    last_digests = repo.get_last_digest(ensemble_id)
    return {
        "messages": [row_to_dict(m) for m in (messages or [])],
        "last_digests": [row_to_dict(d) for d in (last_digests or [])],
    }

@open_router.get("/app/ensembles/{ensemble_id}/medications")
async def app_get_medications(
    ensemble_id: str,
    email: str = Query(...),
):
    """
    Return active medications for all seniors in the ensemble.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    meds = repo.get_medications_for_ensemble(ensemble_id)
    return {"medications": [row_to_dict(m) for m in (meds or [])]}


@open_router.put("/app/people/me")
async def app_update_me(
    email: str = Query(...),
    body: UpdatePersonRequest = ...,
):
    """
    Allow a person to update their own profile fields.
    Scoped to phone, aliases, notes, date_of_birth only — no name or type changes.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    person = repo.update_person(
        person_id=str(row["person_id"]),
        phone=body.phone,
        aliases=body.aliases,
        notes=body.notes,
        date_of_birth=body.date_of_birth,
    )
    return {"person": row_to_dict(person)}


@open_router.put("/app/people/{person_id}")
async def app_update_person(
    person_id: str,
    email: str = Query(...),
    body: UpdatePersonRequest = ...,
):
    """
    Update a person's profile. Admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    person = repo.update_person(
        person_id=person_id,
        name=body.name,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases,
        notes=body.notes,
    )
    return {"person": row_to_dict(person)}


@open_router.put("/app/circles/{circle_id}/people/{person_id}/role")
async def app_update_circle_role(
    circle_id: str,
    person_id: str,
    email: str = Query(...),
    body: CreateCircleMembershipRequest = ...,
):
    """
    Update a person's role in a specific circle. Admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
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
    email: str = Query(...),
    body: UpdateEnsembleMembershipRequest = ...,
):
    """
    Update a person's user role in an ensemble. Admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
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
    email: str = Query(...),
):
    """
    Return digest history for the ensemble.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    digests = repo.get_digest_history(ensemble_id)
    return {"digests": [row_to_dict(d) for d in (digests or [])]}


@open_router.post("/app/ensembles/{ensemble_id}/invite")
async def app_invite_person(
    ensemble_id: str,
    email: str = Query(...),
    body: InvitePersonRequest = ...,
):
    """
    Create (or update) a person in the ensemble and add them to a circle.
    Admin-only. Idempotent — safe to call again if the person already exists.
    Returns the person row and the invite URL.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if body.user_role not in ('admin', 'member'):
        raise HTTPException(status_code=400, detail="user_role must be 'admin' or 'member'")
    if body.care_role not in ('senior', 'family', 'friend', 'caregiver'):
        raise HTTPException(status_code=400, detail="Invalid care_role")

    person = repo.invite_person_to_ensemble(
        ensemble_id=ensemble_id,
        circle_id=body.circle_id,
        name=body.name,
        email=body.email,
        phone=body.phone,
        care_role=body.care_role,
        user_role=body.user_role,
    )
    invite_url = f"https://app.takefive.care?email={body.email}"
    return {"person": row_to_dict(person), "invite_url": invite_url}


@open_router.get("/app/ensembles/{ensemble_id}/clinical-records")
async def app_get_clinical_records(
    ensemble_id: str,
    email: str = Query(...),
    resource_type: Optional[str] = Query(None),
):
    """
    Return clinical records (medications, care team) for all seniors
    in the ensemble visible to the requester.
    Readable by all members; writes are admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    records = repo.get_clinical_records_for_ensemble(
        ensemble_id=ensemble_id,
        resource_type=resource_type,
    )
    return {"records": [row_to_dict(r) for r in (records or [])]}


@open_router.post("/app/ensembles/{ensemble_id}/clinical-records")
async def app_create_clinical_record(
    ensemble_id: str,
    email: str = Query(...),
    body: CreateClinicalRecordRequest = ...,
):
    """
    Create a clinical record (medication or care team member) for a senior
    in the ensemble. Admin-only.
    """
    row = repo.lookup_person_by_email(email)
    if not row or str(row["ensemble_id"]) != ensemble_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
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
    email: str = Query(...),
    body: UpdateClinicalRecordRequest = ...,
):
    """
    Update a clinical record. Admin-only. We verify admin via email lookup;
    we trust that the record belongs to this ensemble since record_ids are UUIDs.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
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
    email: str = Query(...),
):
    """
    Return the roster for a specific circle.
    Visible to all members in the circle; admins can see any circle.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    roster = repo.fetch_circle_roster(circle_id)
    return {"roster": [row_to_dict(r) for r in (roster or [])]}


@open_router.post("/app/circles/{circle_id}/members")
async def app_add_circle_member(
    circle_id: str,
    email: str = Query(...),
    body: CreateCircleMembershipRequest = ...,
):
    """
    Add an existing ensemble person to a circle with a role. Admin-only.
    person_id must be supplied in the request body.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
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
    email: str = Query(...),
):
    """
    Remove a person from a circle. Admin-only.
    Does not delete the person from the ensemble.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    repo.remove_person_from_circle(circle_id=circle_id, person_id=person_id)
    return {"removed": True}


@open_router.get("/app/npi/search")
async def app_npi_search(
    email: str = Query(...),
    first_name: str = Query(...),
    last_name: str = Query(...),
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
):
    """
    NPI registry search proxy for the ensemble admin page.
    Requires a valid email (no admin check — members can look up providers).
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    results = await search_npi(first_name, last_name, city, state, enumeration_type="NPI-1")
    return {"results": results}


@open_router.get("/app/circles/{circle_id}/prep-packets")
async def app_get_prep_packets(
    circle_id: str,
    email: str = Query(...),
):
    """
    Return previously generated prep packets for a circle.
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    packets = repo.get_prep_packets(circle_id)
    return {"packets": [row_to_dict(p) for p in (packets or [])]}


@open_router.post("/app/circles/{circle_id}/prep-packet")
async def app_generate_prep_packet(
    circle_id: str,
    email: str = Query(...),
    body: dict = Body(...),
):
    """
    Generate an appointment prep packet and post it to GroupMe.
    Accessible to all circle members (not admin-only).
    """
    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")

    doctor_name      = body.get("doctor_name", "the doctor")
    appointment_desc = body.get("appointment_desc", "upcoming appointment")

    # Build a fallback question string in case generate_prep_packet ever needs
    # to fall back to free-text parsing (it won't here, since doctor_name and
    # appointment_desc are passed in directly and Step 1's parse is skipped).
    question = f"prep for appointment with {doctor_name} {appointment_desc}"

    try:
        packet_text, followup_text = await generate_prep_packet(
            question=question,
            circle_id=circle_id,
            sender_person_id=str(row["person_id"]),
            doctor_name=doctor_name,
            appointment_desc=appointment_desc,
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


@open_router.post("/app/people/{person_id}/sms-invite")
async def app_sms_invite(
    person_id: str,
    email: str = Query(...),
    circle_id: Optional[str] = Query(None),
):
    """
    Send an SMS invite to a person so they have the Take Five number saved.
    Logs the outbound message to messages as an agent_note.
    Admin-only.
    """
    from twilio.rest import Client as TwilioClient

    row = repo.lookup_person_by_email(email)
    if not row:
        raise HTTPException(status_code=403, detail="Forbidden")
    if row["user_role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    # Load person
    person = repo.get_person_by_id(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    if not person.get("phone"):
        raise HTTPException(status_code=400, detail="Person has no phone number")

    # Find their circle and get the Twilio number
    circles = repo.list_circles_for_person(
        ensemble_id=str(row["ensemble_id"]),
        person_id=person_id,
        user_role="admin",  # fetch all circles so we can find the right one
    )
    # Use specified circle_id if provided, otherwise pick the first SMS-enabled circle
    if circle_id:
        twilio_circle = next(
            (c for c in (circles or []) if str(c["id"]) == circle_id
             and (c.get("integration_config") or {}).get("twilio_number")),
            None,
        )
        if not twilio_circle:
            raise HTTPException(status_code=400, detail="Circle not found or has no SMS number")
    else:
        twilio_circle = next(
            (c for c in (circles or []) if (c.get("integration_config") or {}).get("twilio_number")),
            None,
        )
    if not twilio_circle:
        raise HTTPException(status_code=400, detail="No SMS-enabled circle found for this ensemble")

    twilio_number = twilio_circle["integration_config"]["twilio_number"]
    circle_name = twilio_circle["name"]

    # Find the senior's name for a personalised message
    seniors = repo.get_seniors_in_circle(str(twilio_circle["id"]))
    senior_name = seniors[0]["name"].split()[0] if seniors else "your loved one"

    invite_body = (
        f"Hi {person['name'].split()[0]} - this is Take Five for {circle_name}. "
        f"Text this number after visits with {senior_name} and we'll keep the family in the loop."
    )

    # Send via Twilio
    try:
        twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN"),
        )
        twilio_client.messages.create(
            body=invite_body,
            from_=twilio_number,
            to=person["phone"],
        )
    except Exception as e:
        logger.error(f"[sms-invite] Failed to send to {person['name']}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")

    # Log to messages as agent_note so it appears in activity
    circle_ext_id = twilio_circle["external_id"]
    log_body = f"SMS invite sent to {person['name']} ({person['phone']})"
    repo.log_message(
        circle_ext_id=circle_ext_id,
        person_ext_id=None,
        body=log_body,
        msg_type="agent_note",
        direction="outbound",
        raw_data={"type": "sms_invite", "to_person_id": person_id, "to_phone": person["phone"]},
        channel="sms",
    )

    logger.info(f"[sms-invite] Sent to {person['name']} ({person['phone']}) for circle {circle_name}")
    return {"sent": True, "to": person["phone"], "person": person["name"]}


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
