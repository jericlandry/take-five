import os
from datetime import datetime
from typing import Dict, List
from uuid import UUID

from dotenv import load_dotenv

load_dotenv()

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

RESPONSE_FORMATS = {
    "markdown": "Format your response using markdown — headers, bold, bullet points where appropriate.",
    "text":     "Format your response as plain text only. No markdown, no asterisks, no headers. Use simple line breaks.",
    "json":     "Format your response as a JSON object with keys: 'summary' (string), 'details' (list of strings), 'flags' (list of any concerns worth raising).",
}

CHANNEL_CONSTRAINTS = {
    "groupme": "Keep your response under 600 characters. If the topic warrants more depth, give a focused answer and offer to continue.",
    "sms":     "Keep your response under 300 characters.",
}


def get_prompt(name: str) -> str:
    """
    Returns the raw prompt template string for `name`.

    Currently reads from take_five/prompts/{name}.md. Callers do their own
    variable substitution via .format(**kwargs) — this function never does
    any templating itself, just returns the string.

    To switch back to LangSmith Hub, replace this function's body with:

        from langsmith import Client
        _ls_client = Client()

        def get_prompt(name: str) -> str:
            hub_name = name.replace("_", "-")
            pulled = _ls_client.pull_prompt(hub_name)
            return pulled.messages[0].prompt.template

    No caller anywhere needs to change — they only ever depend on getting
    a plain string back from get_prompt().
    """
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    with open(path, "r") as f:
        return f.read()


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