import logging
import httpx

GROUPME_URL = "https://api.groupme.com/v3/bots/post"
GROUPME_HEADERS = {
    "User-Agent": "curl/7.68.0",
    "Content-Type": "application/json"
}

def send_message(bot_id: str, text: str) -> bool:
    """Send a message to a GroupMe bot. Returns True on success.
    
    bot_id comes from care_circles.integration_config['groupme_bot_id'].
    """
    with httpx.Client() as client:
        response = client.post(
            GROUPME_URL,
            json={"bot_id": bot_id, "text": text},
            headers=GROUPME_HEADERS
        )
    if response.status_code == 202:
        logging.info("Message sent successfully to GroupMe")
        return True
    logging.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
    return False

async def send_message_async(bot_id: str, text: str) -> bool:
    """Async version for use inside the FastAPI webhook.
    
    bot_id comes from care_circles.integration_config['groupme_bot_id'].
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            GROUPME_URL,
            json={"bot_id": bot_id, "text": text},
            headers=GROUPME_HEADERS
        )
    if response.status_code == 202:
        logging.info("Message sent successfully to GroupMe")
        return True
    logging.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")
    return False