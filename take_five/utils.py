from datetime import datetime
from typing import List, Dict

from dotenv import load_dotenv

from langsmith import Client

from uuid import UUID
import os

load_dotenv()  # Load environment variables from .env file

ls_client = Client()

RESPONSE_FORMATS = {
    "markdown": "Format your response using markdown — headers, bold, bullet points where appropriate.",
    "text":     "Format your response as plain text only. No markdown, no asterisks, no headers. Use simple line breaks.",
    "json":     "Format your response as a JSON object with keys: 'summary' (string), 'details' (list of strings), 'flags' (list of any concerns worth raising).",
}

def fetch_prompt(prompt_name):
    """
    Pull the named prompt from LangSmith Hub and return a runnable chain
    ready to invoke with Claude Haiku.
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