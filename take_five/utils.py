from datetime import datetime
from typing import List, Dict

from langsmith import Client

import json
from uuid import UUID

from take_five.summaries import SUMMARY_PROMPT_NAME

ls_client = Client()

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