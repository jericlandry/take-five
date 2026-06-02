import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Security, HTTPException, Depends, APIRouter, Request, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from take_five.integrations.groupme import handle_groupme_webhook
from take_five.integrations.npi import search_npi
from take_five.integrations.twilio import handle_sms
from take_five.messages import ask_with_tools
from take_five.repository import repo
from take_five.schemas import (
    CreatePersonRequest, UpdatePersonRequest,
    CreateCareCircleRequest, UpdateCareCircleRequest,
    CreateCircleMembershipRequest,
    CreateEnsembleRequest,
    CreateClinicalRecordRequest, UpdateClinicalRecordRequest,
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
        p_type=body.p_type,
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
        p_type=body.p_type,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases,
        notes=body.notes,
        external_id=body.external_id,
        date_of_birth=body.date_of_birth,
    )
    return {"person": row_to_dict(person)}

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


@open_router.get("/admin/{file_name}")
async def read_admin(file_name: str):
    return FileResponse(f'admin/{file_name}')

app.include_router(open_router)
