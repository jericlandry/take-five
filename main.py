from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import FileResponse

import logging

logging.basicConfig(level=logging.INFO)
app = FastAPI()

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

    # ignore the bot's own messages
    if data.get("sender_type") == "bot":
        logging.info("Bot message ignored")
        return {"status": "ignored"}
    
    # store raw, then classify async
    #await store_message(data)
    #background_tasks.add_task(classify_and_extract, data)
    
    logging.info("Webhook processed successfully")
    return {"status": "ok"}