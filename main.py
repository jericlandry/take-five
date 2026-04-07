from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

import logging

from take_five.repository import TakeFiveRepository

logging.basicConfig(level=logging.INFO)

repo = TakeFiveRepository({
    'dbname': 'takefive',
    'user': 'jeric',
    'password': 'M7CzRtB67FcmZj6kwBv04zYy5eDwv7xN',
    'host': 'dpg-d78po2h5pdvs73b7l7rg-a.virginia-postgres.render.com',
    'port': 5432
}) 

app = FastAPI()

GROUP_NAME = "Take Five Ensemble"

@app.get("/health")
async def health():
    logging.info("Health check requested")
    return {"status": "ok"}

@app.get("/")
async def read_index():
    logging.info("Index page requested")
    return FileResponse('website/index.html')

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
        
        logging.info(f"Message stored. Internal ID: {new_msg['id']}")

    except Exception as e:
        logging.error(f"Failed to sync or log message: {e}")
        # Returning "ok" prevents GroupMe from retrying a broken request repeatedly
        return {"status": "error", "message": str(e)}

    logging.info("Webhook processed successfully")

    return {"status": "ok"}
