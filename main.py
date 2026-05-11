import asyncio

from fastapi import FastAPI, Security, HTTPException, Depends, APIRouter,  Request, Form, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
import logging
import httpx
import os
from datetime import datetime

from pydantic import BaseModel
from typing import Optional, List
from starlette.responses import FileResponse
from twilio.twiml.messaging_response import MessagingResponse

from take_five.memory import process_message_for_memory
from take_five.repository import TakeFiveRepository
from take_five.summaries import generate_weekly_digest
from take_five.messages import ask
from take_five.utils import row_to_dict, row_list_to_dict_list

logging.basicConfig(level=logging.INFO)

load_dotenv()  # Load environment variables from .env file

security = HTTPBearer()

TAKE_FIVE_ADMIN_API_KEY = os.getenv("TAKE_FIVE_ADMIN_API_KEY")
if not TAKE_FIVE_ADMIN_API_KEY:
    logging.warning("TAKE_FIVE_ADMIN_API_KEY not set in environment variables. Admin endpoints will be unsecured.")

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
        "http://127.0.0.1:8000",
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
GROUP_NAME = "Take Five Ensemble"
GROUPME_BOT_ID = "f7a1dcd219899a79f3d01dec91"
GROUPME_URL = "https://api.groupme.com/v3/bots/post"

class CreatePersonRequest(BaseModel):
    name: str
    p_type: str
    phone: Optional[str] = None
    email: Optional[str] = None
    aliases: Optional[List[str]] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None

class UpdatePersonRequest(BaseModel):
    name: Optional[str] = None
    p_type: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    aliases: Optional[List[str]] = None
    notes: Optional[str] = None
    external_id: Optional[str] = None

class CreateCareCircleRequest(BaseModel):
    name: str
    status: str = 'active'
    external_id: Optional[str] = None

class CreateCircleMembershipRequest(BaseModel):
    role: str  # senior | family | caregiver | professional

class CreateEnsembleRequest(BaseModel):
    name: str
    plan: str
    status: str = "trial"

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
    logging.info("Digest request received")
    kwargs = {}
    if body.start_date: # date format for reuquest ex: "2026-05-01T00:00:00"
        kwargs['start_date'] = body.start_date
    if body.end_date:
        kwargs['end_date'] = body.end_date
    digest = generate_weekly_digest(body.circle_id, **kwargs)
    return {"digest": digest}

@secure_router.post("/ensembles")
async def create_ensemble(body: CreateEnsembleRequest):
    logging.info(f"Create ensemble request received: {body.name}")
    ensemble = repo.create_ensemble(body.name, body.plan, body.status)
    return {"ensemble": row_to_dict(ensemble)}

@secure_router.get("/ensembles")
async def get_ensembles():
    logging.info("Get ensembles request received")
    ensembles = repo.list_ensembles()
    return {"ensembles": row_list_to_dict_list(ensembles)}

@secure_router.get("/ensembles/{ensemble_id}/people")
async def get_ensemble_people(ensemble_id: str):
    logging.info(f"Get people for ensemble {ensemble_id}")
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
        notes=body.notes
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

@secure_router.post("/messages")
async def message(body: MessageRequest):
    logging.info("Message received")
    response = await ask(body.message, body.circle_id, response_format=body.response_format)
    return {"response": response}

#--- Open endpoints for external integrations (e.g. GroupMe, Twilio) ---
@open_router.post("/groupme/webhook")
async def groupme_webhook(request: Request):
    data = await request.json()
    logging.info("GroupMe webhook received")

    # 1. Guard: Ignore the bot's own messages to avoid infinite loops
    if data.get("sender_type") == "bot":
        logging.info("Bot message ignored")
        return {"status": "ignored"}
    
    # 2. Extract GroupMe specific fields
    circle_ext_id = f"groupme:{data.get('group_id')}"
    person_ext_id = f"groupme:{data.get('sender_id')}"
    person_name = data.get("name", "Unknown User")
    text = data.get("text", "")

    logging.info(f"Processing message from {person_name} in group {circle_ext_id}")

    try:

        new_msg = repo.log_message(
            circle_ext_id=circle_ext_id,
            person_ext_id=person_ext_id,
            body=text,
            raw_data=data,  # Full JSON goes into the 'raw' column
            channel="groupme"
        )

        asyncio.create_task(process_message_for_memory(
            message_id=str(new_msg['id']),
            circle_id=str(new_msg['circle_id']),
            body=text,
            sender=person_name,
            sent_at=new_msg['sent_at'],
            repo=repo
        ))

        if '@T5' in text:
            question = text.split('@T5', 1)[1].strip()
            circle_id = repo.get_circle_by_external_id(circle_ext_id)['id']
            
            if not question:
                logging.warning("T5 command detected but no question found.")
                return {"status": "ok"}
            if not circle_id:
                logging.error(f"Circle with external_id {circle_ext_id} not found in database.")
                return {"status": "ok"}
            
            logging.info(f"T5 question command detected, generating digest...")
            bot_response = await ask(question, circle_id, response_format="text")
            
            # Define headers to match what worked in curl
            headers = {
                "User-Agent": "curl/7.68.0", # Mimics the successful curl request
                "Content-Type": "application/json"
            }
            
            async with httpx.AsyncClient() as client:
                payload = {
                    "bot_id": GROUPME_BOT_ID,
                    "text": bot_response
                }
                
                # We add 'params' to the request to authenticate
                response = await client.post(GROUPME_URL, json=payload, headers=headers)
                
                if response.status_code == 202:
                    logging.info("Digest sent successfully to GroupMe")
                else:
                    # GroupMe errors often contain helpful JSON in response.text
                    logging.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
            # --------------------------------
        
        logging.info(f"Message stored. Internal ID: {new_msg['id']}")

    except Exception as e:
        logging.error(f"Failed to sync or log message: {e}")
        # Returning "ok" prevents GroupMe from retrying a broken request repeatedly
        return {"status": "error", "message": str(e)}

    logging.info("Webhook processed successfully")

    return {"status": "ok"}

@open_router.post("/twilio/sms")
async def receive_sms(From: str = Form(...), Body: str = Form(...), To: str = Form(...)):
    response = MessagingResponse()
    
    person = repo.find_person_by_phone(From)
    circle_ext_id = f"sms:{To}"
    person_ext_id = f"sms:{From}"

    if not person:
        logging.info(f"SMS received from unknown number: {From}")
        response.message("Sorry, I don't recognize your number. Please contact support to be added to the system.")
        return Response(content=str(response), media_type="application/xml")

    logging.info(f"SMS received from known person: {person['name']} ({From})")
    circle = repo.get_circle_by_external_id(circle_ext_id)

    if not circle:
        logging.warning(f"No circles found for person {person['name']} ({From})")
        response.message("Sorry, I couldn't find your care circle. Please contact support.")
        return Response(content=str(response), media_type="application/xml")

    new_msg = repo.log_message(
        circle_ext_id=circle_ext_id,
        person_ext_id=person_ext_id,
        body=Body,
        raw_data={"from": From, "to": To, "body": Body},
        channel="sms"
    )

    asyncio.create_task(process_message_for_memory(
        message_id=str(new_msg['id']),
        circle_id=str(new_msg['circle_id']),
        body=Body,
        sender=person['name'],
        sent_at=new_msg['sent_at'],
        repo=repo
    ))

    logging.info(f"Twilio SMS logged from {person['name']}: '{Body}'")
    response.message(f"Got it, {person['name']}. Thanks for the update.")
    return Response(content=str(response), media_type="application/xml")

app.include_router(secure_router)

@open_router.get("/health")
async def health():
    logging.info("Health check requested")
    return {"status": "ok"}

@open_router.get("/admin/{file_name}")
async def read_admin(file_name: str):
    return FileResponse(f'admin/{file_name}')

app.include_router(open_router)