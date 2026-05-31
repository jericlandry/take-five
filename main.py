import asyncio
import httpx

from fastapi import FastAPI, Security, HTTPException, Depends, APIRouter, Request, Form, Response, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
import logging
import os
from datetime import datetime

from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from starlette.responses import FileResponse
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import AsyncAnthropic

from take_five.repository import TakeFiveRepository
from take_five.summaries import generate_weekly_digest
from take_five.messages import ask_with_tools
from take_five.utils import row_to_dict, row_list_to_dict_list
from take_five.images import extract_sms_image, handle_image_message

from take_five.integrations.groupme import groupme_reply, handle_groupme_webhook

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

repo = TakeFiveRepository()

GROUPME_ACCESS_TOKEN = os.getenv("GROUPME_ACCESS_TOKEN")

class CreatePersonRequest(BaseModel):
    name: str
    p_type: str
    phone: Optional[str] = None
    email: Optional[str] = None
    aliases: Optional[List[str]] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None
    date_of_birth: Optional[str] = None

class UpdatePersonRequest(BaseModel):
    name: Optional[str] = None
    p_type: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    aliases: Optional[List[str]] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None
    date_of_birth: Optional[str] = None

class CreateCareCircleRequest(BaseModel):
    name: str
    status: str = 'active'
    external_id: Optional[str] = None

class CreateCircleMembershipRequest(BaseModel):
    role: str  # senior | family | caregiver | professional

class UpdateCareCircleRequest(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    external_id: Optional[str] = None
    integration_config: Optional[dict] = None

class CreateEnsembleRequest(BaseModel):
    name: str
    plan: str
    status: str = "trial"

class UpdateClinicalRecordRequest(BaseModel):
    data: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    status: Optional[str] = None

class CreateClinicalRecordRequest(BaseModel):
    person_id: str
    resource_type: str
    data: Dict[str, Any]
    notes: Optional[str] = None
    status: str = 'active'

class MessageRequest(BaseModel):
    circle_id: str
    message: str
    response_format: str = "markdown"

class DigestRequest(BaseModel):
    circle_id: str
    response_format: str = "markdown"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None

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
    person = repo.update_person(person_id, body)
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
):
    params = {
        "first_name": first_name,
        "last_name": last_name,
        "limit": 5,
        "enumeration_type": "NPI-1",
        "version": "2.1",
    }
    if city:
        params["city"] = city
    if state:
        params["state"] = state

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://npiregistry.cms.hhs.gov/api/",
            params=params,
            timeout=10,
        )
    resp.raise_for_status()
    raw = resp.json()

    results = []
    for r in raw.get("results", []):
        basic      = r.get("basic", {})
        taxonomies = r.get("taxonomies", [])
        primary    = next((t for t in taxonomies if t.get("primary")), None)
        # Fall back to any taxonomy with a description if primary has none
        if not primary or not primary.get("desc"):
            primary = next((t for t in taxonomies if t.get("desc")), primary or {})
        addresses  = r.get("addresses", [])
        practice   = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), addresses[0] if addresses else {})
        results.append({
            "npi":           r.get("number"),
            "name":          f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip(),
            "credential":    basic.get("credential", ""),
            "specialty":     primary.get("desc", ""),
            "taxonomy_code": primary.get("code", ""),
            "phone":         practice.get("telephone_number", ""),
            "address":       f"{practice.get('address_1', '')} {practice.get('address_2', '')}".strip(),
            "city":          practice.get("city", ""),
            "state":         practice.get("state", ""),
            "postal_code":   practice.get("postal_code", ""),
        })

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
    response = MessagingResponse()

    # 1. Identify care circle by the Twilio number that received the message
    circle = repo.get_circle_by_twilio_number(To)
    if not circle:
        logger.warning(f"SMS received on unrecognized Twilio number: {To}")
        response.message("We don't recognize this number. Contact your care circle administrator.")
        return Response(content=str(response), media_type="application/xml")

    circle_id     = str(circle['id'])
    circle_ext_id = circle['external_id']  # use the circle's real external_id for logging

    # 2. Identify sender — must be an sms_active member of this specific circle
    person = repo.find_caregiver_by_phone_and_circle(From, circle_id)

    if not person:
        logger.warning(f"SMS from unrecognized number {From} for circle {circle['name']}")
        response.message("We don't recognize this number. Contact your care circle administrator.")
        return Response(content=str(response), media_type="application/xml")

    logger.info(f"SMS received from {person['name']} ({From}) for circle {circle['name']}")

    new_msg = repo.log_message(
        circle_ext_id=circle_ext_id,
        person_ext_id=None,
        body=Body,
        raw_data={"from": From, "to": To, "body": Body},
        channel="sms",
        person_id=str(person['id']),
    )

    asyncio.create_task(process_message_for_memory(
        message_id=str(new_msg['id']),
        circle_id=str(new_msg['circle_id']),
        body=Body,
        sender=person['name'],
        sent_at=new_msg['sent_at'],
        repo=repo
    ))

    # Synthesize and post to GroupMe
    bot_id          = (circle.get('integration_config') or {}).get('groupme_bot_id')
    groupme_ext_id  = circle.get('external_id')  # groupme:{group_id}

    if bot_id and groupme_ext_id:
        async def post_caregiver_update():
            try:
                client = AsyncAnthropic()
                result = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    system=(
                        "You summarize caregiver check-in messages for a family care circle. "
                        "Write a single short paragraph (2-3 sentences max). "
                        "Be warm and specific — include what the senior did, how they seemed, "
                        "and anything worth knowing. No bullet points. No greeting. "
                        "Do not invent details not present in the message."
                    ),
                    messages=[{
                        "role": "user",
                        "content": f"{person['name']} checked in: {Body}"
                    }]
                )
                summary = result.content[0].text.strip()
                await groupme_reply(
                    bot_id,
                    f"{person['name']} (via Take Five): {summary}",
                    groupme_ext_id
                )
                logger.info(f"[sms] Caregiver update posted to GroupMe for circle {circle['name']}")
            except Exception as e:
                logger.error(f"[sms] Failed to synthesize or post caregiver update: {e}")
        asyncio.create_task(post_caregiver_update())

    # MMS image detection
    if int(NumMedia) > 0:
        sms_payload = {
            "NumMedia": NumMedia,
            "MediaUrl0": MediaUrl0,
            "MediaContentType0": MediaContentType0,
            "Body": Body, "From": From, "To": To,
            "sender_name": person['name'], "MessageSid": "",
        }
        image_attachment = extract_sms_image(sms_payload)
        if image_attachment:
            async def process_sms_image():
                result = await handle_image_message(image_attachment)
                if result:
                    _reply, _vision_result = result
                    # SMS reply and logging TBD when SMS channel is active
                    logger.info(f"[sms] Image processed — classification: {_vision_result.get('classification')}")
            asyncio.create_task(process_sms_image())

    logger.info(f"Twilio SMS logged from {person['name']}: '{Body}'")
    response.message(f"Got it, {person['name']}. Thanks for the update.")
    return Response(content=str(response), media_type="application/xml")


app.include_router(secure_router)

@open_router.get("/health")
async def health():
    logger.info("Health check requested")
    return {"status": "ok"}

@open_router.get("/admin/{file_name}")
async def read_admin(file_name: str):
    return FileResponse(f'admin/{file_name}')

app.include_router(open_router)
