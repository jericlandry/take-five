import asyncio

from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import FileResponse

from dotenv import load_dotenv
import logging
import httpx
import os

from pydantic import BaseModel
from typing import Optional, List
from twilio.twiml.messaging_response import MessagingResponse

from take_five.memory import process_message_for_memory
from take_five.repository import TakeFiveRepository
from take_five.summaries import generate_weekly_digest
from take_five.messages import ask
from take_five.utils import row_to_dict, row_list_to_dict_list

logging.basicConfig(level=logging.INFO)

load_dotenv()  # Load environment variables from .env file

app = FastAPI()

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
    external_id_type: Optional[str] = None

class CreateCareCircleRequest(BaseModel):
    name: str
    status: str = 'active'
    external_id: Optional[str] = None
    external_type: str = 'groupme'

class CreateCircleMembershipRequest(BaseModel):
    role: str  # senior | family | caregiver | professional

@app.get("/health")
async def health():
    logging.info("Health check requested")
    return {"status": "ok"}

@app.get("/")
async def read_index():
    logging.info("Index page requested")
    return FileResponse('website/index.html')

@app.post("/digest")
async def summary(circle_id: str):
    logging.info("Summary request received")

    digest = generate_weekly_digest(circle_id)

    return {"digest": digest}

@app.post("/ensembles")
async def create_ensemble(name: str, plan: str, status: str = "trial"):
    logging.info("Create ensemble request received")
    ensemble = repo.create_ensemble(name, plan, status)
    return {"ensemble": row_to_dict(ensemble)}

@app.get("/ensembles")
async def get_ensembles():
    logging.info("Get ensembles request received")
    ensembles = repo.list_ensembles()
    return {"ensembles": row_list_to_dict_list(ensembles)}

@app.get("/ensembles/{ensemble_id}/people")
async def get_ensemble_people(ensemble_id: str):
    logging.info(f"Get people for ensemble {ensemble_id}")
    people = repo.list_people_by_ensemble(ensemble_id)
    return {"people": [row_to_dict(row) for row in people]}

@app.post("/ensembles/{ensemble_id}/people")
async def create_person(ensemble_id: str, body: CreatePersonRequest):
    person = repo.add_person_to_ensemble(
        ensemble_id=ensemble_id,
        name=body.name,
        p_type=body.p_type,
        phone=body.phone,
        email=body.email,
        aliases=body.aliases or [],
        notes=body.notes
    )
    return {"person": row_to_dict(person)}

@app.get("/ensembles/{ensemble_id}/circles")
async def get_care_circles(ensemble_id: str):
    circles = repo.list_care_circles(ensemble_id=ensemble_id)
    return {"circles": [row_to_dict(row) for row in circles]}

@app.post("/ensembles/{ensemble_id}/circles")
async def create_care_circle(ensemble_id: str, body: CreateCareCircleRequest):
    circle = repo.create_care_circle(
        ensemble_id=ensemble_id,
        name=body.name,
        status=body.status,
        external_id=body.external_id,
        external_type=body.external_type
    )
    return {"circle": row_to_dict(circle)}

@app.get("/circles/{circle_id}")
async def get_circle_by_id(circle_id: str):
    circle = repo.get_circle_by_id(circle_id)
    return {"people": row_to_dict(circle)}

@app.get("/circles/{circle_id}/people")
async def get_circle_people(circle_id: str):
    people = repo.fetch_circle_roster(circle_id)
    return {"people": [row_to_dict(row) for row in people]}

@app.post("/circles/{circle_id}/people/{person_id}")
async def add_person_to_circle(circle_id: str, person_id: str,
                                body: CreateCircleMembershipRequest):
    membership = repo.add_person_to_circle(
        circle_id=circle_id,
        person_id=person_id,
        role=body.role
    )
    return {"membership": row_to_dict(membership)}

@app.get("/circles/{circle_id}/people")
async def get_circle_people(circle_id: str):
    people = repo.fetch_circle_roster(circle_id)
    return {"people": [row_to_dict(row) for row in people]}

@app.post("/messages")
async def chat(circle_id: str, message: str, response_format: str = "markdown"):
    logging.info("Chat message received")
    response = await ask(message, circle_id, response_format=response_format)
    return {"response": response}

@app.post("/groupme/webhook")
async def groupme_webhook(request: Request):
    data = await request.json()
    logging.info("GroupMe webhook received")

    # 1. Guard: Ignore the bot's own messages to avoid infinite loops
    if data.get("sender_type") == "bot":
        logging.info("Bot message ignored")
        return {"status": "ignored"}
    
    # 2. Extract GroupMe specific fields
    circle_ext_id = data.get("group_id")
    person_ext_id = data.get("sender_id")
    person_name = data.get("name", "Unknown User")
    text = data.get("text", "")

    logging.info(f"Processing message from {person_name} in group {circle_ext_id}")

    try:
        # 3. The "Triple Upsert" 
        # Ensures the Circle, Person, and Membership exist before we log the message
        repo.upsert_circle(circle_ext_id, GROUP_NAME) 
        repo.upsert_person(person_ext_id, person_name, "family")
        repo.add_to_circle(circle_ext_id, person_ext_id, "family")

        # 4. Log the message with the raw GroupMe payload
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

@app.post("/twilio/sms")
async def receive_sms(From: str = Form(...), Body: str = Form(...)):
    # 1. Start TwiML response
    response = MessagingResponse()
    
    person = repo.find_person_by_phone(From)

    if person:
        logging.info(f"SMS received from known person: {person['name']} ({From})")
        circles = repo.find_circles_by_person(person['external_id']) if person else []
        if circles:
            person_ext_id = person['external_id']
            circle_ext_id = circles['external_id']  # Assuming one circle per person for simplicity
            # 4. Log the message with the raw GroupMe payload
            new_msg = repo.log_message(
                circle_ext_id=circle_ext_id,
                person_ext_id=person_ext_id,
                body=Body,
                raw_data=Body,
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
        else:
            logging.warning(f"No circles found for person {person['name']} ({From})")
            response.message("Sorry, I couldn't find your care circle. Please contact support.")
            return Response(content=str(response), media_type="application/xml")
    else:
        logging.info(f"SMS received from unknown number: {From}")
        response.message("Sorry, I don't recognize your number. Please contact support to be added to the system.")
        return Response(content=str(response), media_type="application/xml")
    
    # 2. Logic: Print the message and reply
    logging.info(f"Twilio SMS received from {From}: {Body}")

    response.message(f"Hey! {person['name']} I got your message: '{Body}'")

    # 3. Return as XML
    return Response(content=str(response), media_type="application/xml")

