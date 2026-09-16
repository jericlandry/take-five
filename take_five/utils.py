import os
from datetime import datetime, timedelta
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


def build_calendar_context(start_date: datetime, end_date: datetime, lookback_days: int = 3) -> str:
    """
    Deterministic day-name -> date lookup table, so an LLM can resolve a
    relative day mentioned in a message ("we did X Friday") by lookup
    instead of computing weekday arithmetic itself -- found doing this
    arithmetic wrong in production (labeled a message's "Friday" reference
    as August 22, 2026, which is actually a Saturday -- see Kathy Landry /
    Landry F&F digest, 2026-08-26). Also used to fix the same class of bug
    in the senior email's Life Log excerpt, which echoed a bare "Monday"
    from a source message with no date resolution at all (see Dr. Kalif
    appointment confusion, 2026-09-16).

    Shared by generate_weekly_digest()/generate_outer_weekly_digest() in
    summaries.py and extract_life_log_topic()'s recent-thread extraction in
    engagement/life_log.py -- lives here rather than in summaries.py so
    life_log.py can import it without a circular import (summaries.py
    already imports from engagement.life_log).

    lookback_days extends the table before start_date so a message sent
    early in the window referencing a day just before it can still resolve
    correctly, without pulling in a full extra week.
    """
    calendar_start = (start_date - timedelta(days=lookback_days)).date()
    calendar_end = end_date.date()
    lines = ["## Calendar Reference\n",
             "Use this table to resolve any day name mentioned in a message "
             "(e.g. \"Friday\") to its actual date. Do not compute weekday "
             "arithmetic yourself -- look it up here.\n"]
    d = calendar_start
    while d <= calendar_end:
        lines.append(f"- {d.strftime('%A, %B %d, %Y')}")
        d += timedelta(days=1)
    return "\n".join(lines)


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
        if isinstance(val, list): return [convert(v) for v in val]
        return val
    return {key: convert(val) for key, val in row.items()}


def row_list_to_dict_list(rows) -> List[Dict]:
    """Converts a list of RealDictRows to a list of plain dicts."""
    return [row_to_dict(row) for row in rows]