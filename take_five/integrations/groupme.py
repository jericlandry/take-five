import os
import logging
import httpx

GROUPME_BOT_ID = os.getenv("GROUPME_BOT_ID", "f7a1dcd219899a79f3d01dec91")
GROUPME_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_HEADERS = {
    "User-Agent": "curl/7.68.0",
    "Content-Type": "application/json"
}

def send_message(text: str) -> bool:
    """Send a message to the GroupMe bot. Returns True on success."""
    with httpx.Client() as client:
        response = client.post(
            GROUPME_URL,
            json={"bot_id": GROUPME_BOT_ID, "text": text},
            headers=GROUPME_HEADERS
        )
    if response.status_code == 202:
        logging.info("Message sent successfully to GroupMe")
        return True
    logging.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
    return False

async def send_message_async(text: str) -> bool:
    """Async version for use inside the FastAPI webhook."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            GROUPME_URL,
            json={"bot_id": GROUPME_BOT_ID, "text": text},
            headers=GROUPME_HEADERS
        )
    if response.status_code == 202:
        logging.info("Message sent successfully to GroupMe")
        return True
    logging.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
    return False