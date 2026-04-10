from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import FileResponse

from dotenv import load_dotenv
import logging
import httpx
import os

from twilio.twiml.messaging_response import MessagingResponse

from take_five.repository import TakeFiveRepository
from take_five.summaries import generate_weekly_digest

logging.basicConfig(level=logging.INFO)

load_dotenv()  # Load environment variables from .env file

app = FastAPI()

repo = TakeFiveRepository() 

GROUPME_ACCESS_TOKEN = os.getenv("GROUPME_ACCESS_TOKEN")
GROUP_NAME = "Take Five Ensemble"
GROUPME_BOT_ID = "f7a1dcd219899a79f3d01dec91"
GROUPME_URL = "https://api.groupme.com/v3/bots/post"

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
            raw_data=data  # Full JSON goes into the 'raw' column
        )

        if '@T5' in text:
            logging.info("Summary command detected, generating digest...")
            digest = generate_weekly_digest(circle_ext_id)
            
            # Define headers to match what worked in curl
            headers = {
                "User-Agent": "curl/7.68.0", # Mimics the successful curl request
                "Content-Type": "application/json"
            }
            
            async with httpx.AsyncClient() as client:
                payload = {
                    "bot_id": GROUPME_BOT_ID,
                    "text": digest
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
                raw_data=Body
            )
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

