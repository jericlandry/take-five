
from main import GROUPME_BOT_ID, GROUPME_URL
from take_five.summaries import generate_weekly_digest
from dotenv import load_dotenv
import httpx
import logging

load_dotenv()
logging.basicConfig(level=logging.INFO)

def main(circle_ext_id: str):
    digest = generate_weekly_digest(circle_ext_id)
    with httpx.Client() as client:
        headers = {
            "User-Agent": "curl/7.68.0", # Mimics the successful curl request
            "Content-Type": "application/json"
        }

        payload = {
            "bot_id": GROUPME_BOT_ID,
            "text": digest
        }
        
        # We add 'params' to the request to authenticate
        response = client.post(GROUPME_URL, json=payload, headers=headers)
        
        if response.status_code == 202:
            logging.info("Digest sent successfully to GroupMe")
        else:
            # GroupMe errors often contain helpful JSON in response.text
            logging.error(f"Failed to send to GroupMe: {response.status_code} - {response.text}")

if __name__ == "__main__":
    main("114182896")
    
