from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from dotenv import load_dotenv
import logging
import httpx

from take_five.repository import TakeFiveRepository
from take_five.summaries import generate_weekly_digest

logging.basicConfig(level=logging.INFO)

load_dotenv()  # Load environment variables from .env file

app = FastAPI()

repo = TakeFiveRepository() 

GROUP_NAME = "Take Five Ensemble"
GROUPME_BOT_ID = "f7a1dcd219899a79f3d01dec91"

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

        if text.strip().lower() == 't5summary':
            logging.info("Summary command detected, generating digest...")
            digest = generate_weekly_digest(circle_ext_id)
            
            # --- SEND RESPONSE TO GROUPME ---
            async with httpx.AsyncClient() as client:
                payload = {
                    "bot_id": GROUPME_BOT_ID,
                    "text": digest
                }
                response = await client.post("https://groupme.com", json=payload)
                
                if response.status_code == 202:
                    logging.info("Digest sent successfully to GroupMe")
                else:
                    logging.error(f"Failed to send to GroupMe: {response.text}")
            # --------------------------------
        
        logging.info(f"Message stored. Internal ID: {new_msg['id']}")

    except Exception as e:
        logging.error(f"Failed to sync or log message: {e}")
        # Returning "ok" prevents GroupMe from retrying a broken request repeatedly
        return {"status": "error", "message": str(e)}

    logging.info("Webhook processed successfully")

    return {"status": "ok"}
