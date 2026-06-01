from datetime import datetime
from typing import Dict, List
from uuid import UUID

from dotenv import load_dotenv
from langsmith import Client

load_dotenv()

ls_client = Client()

RESPONSE_FORMATS = {
    "markdown": "Format your response using markdown — headers, bold, bullet points where appropriate.",
    "text":     "Format your response as plain text only. No markdown, no asterisks, no headers. Use simple line breaks.",
    "json":     "Format your response as a JSON object with keys: 'summary' (string), 'details' (list of strings), 'flags' (list of any concerns worth raising).",
}

CHANNEL_CONSTRAINTS = {
    "groupme": "Keep your response under 600 characters. If the topic warrants more depth, give a focused answer and offer to continue.",
    "sms":     "Keep your response under 300 characters.",
}


def fetch_prompt(prompt_name: str):
    """
    Pull the named prompt from LangSmith Hub.
    Used for both digest generation (t5-week-summary) and
    memory chunking (chunk-context-summary).
    """
    return ls_client.pull_prompt(prompt_name)


def row_to_dict(row) -> dict:
    """Converts a RealDictRow to a plain dict with serializable types."""
    def convert(val):
        if isinstance(val, UUID): return str(val)
        if isinstance(val, datetime): return val.isoformat()
        return val
    return {key: convert(val) for key, val in row.items()}


def row_list_to_dict_list(rows) -> List[Dict]:
    """Converts a list of RealDictRows to a list of plain dicts."""
    return [row_to_dict(row) for row in rows]