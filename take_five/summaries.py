import os
from datetime import datetime, timedelta
from typing import List, Dict
 
from langsmith import Client
from langchain_anthropic import ChatAnthropic
 
from take_five.repository import TakeFiveRepository

SUMMARY_PROMPT_NAME   = os.getenv("LANGSMITH_PROMPT_NAME", "t5-week-summary")
ANTHROPIC_MODEL       = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_MAX_TOKENS  = int(os.getenv("ANTHROPIC_MAX_TOKENS", "600"))
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY")

# 1. Fetch messages from Supabase
repo = TakeFiveRepository() 
 
def format_conversation(messages: List[Dict]) -> str:
    """
    Convert raw message dicts from TakeFiveRepository into the
    'sender (timestamp): message' format that works best for Haiku.
 
    Expects each dict to have at minimum:
        sender_name     : str
        formatted_time  : str  (or created_at / timestamp as fallback)
        message         : str  (or content / text as fallback)
    """
    lines = []
    for msg in messages:
        sender    = msg.get("author_name") or msg.get("name") or msg.get("sender") or "Unknown"
        timestamp = (
            msg.get("formatted_time")
            or msg.get("created_at")
            or msg.get("timestamp")
            or ""
        )
        content   = (
            msg.get("message")
            or msg.get("content")
            or msg.get("text")
            or msg.get("body")
            or ""
        ).strip()
 
        if not content:
            continue
 
        prefix = f"{sender} ({timestamp}): " if timestamp else f"{sender}: "
        lines.append(f"{prefix}{content}")
 
    return "\n\n".join(lines)

def fetch_prompt(conversation: str):
    """
    Pull the named prompt from LangSmith Hub and return a runnable chain
    ready to invoke with Claude Haiku.
    """
    ls_client = Client()
    prompt_template = ls_client.pull_prompt(SUMMARY_PROMPT_NAME)
 
    llm = ChatAnthropic(
        model=ANTHROPIC_MODEL,
        max_tokens=ANTHROPIC_MAX_TOKENS
    )
 
    return prompt_template | llm


# ---------------------------------------------------------------------------
# Generate digest
# ---------------------------------------------------------------------------
 
def generate_weekly_digest(
    circle_ext_id: str,
    start_date: datetime=datetime.utcnow() - timedelta(days=7),
    end_date: datetime=datetime.utcnow(),
) -> str:
    """
    Fetch the past week of messages for a care circle, run them through
    the t5-week-summary prompt, and return the digest as a string.
 
    Args:
        circle_ext_id:  External ID of the care circle (GroupMe group ID).
        start_date:     Start of the date range (defaults to 7 days ago).
        end_date:       End of the date range (defaults to now).
 
    Returns:
        The generated weekly digest as a plain string.
    """
    # Default to the past 7 days
    if end_date is None:
        end_date = datetime.utcnow()
    if start_date is None:
        start_date = end_date - timedelta(days=7)

    messages = repo.get_messages_in_date_range(
        circle_ext_id=circle_ext_id,
        start_date=start_date,
        end_date=end_date
    )

    if not messages:
        return "No messages found for this period — nothing to summarise."
    
    # 2. Format conversation for the model
    conversation = format_conversation(messages)
 
    # 3. Build chain (prompt pulled from LangSmith | Claude Haiku)
    chain = fetch_prompt(conversation)
 
    # 4. Invoke and return the digest text
    response = chain.invoke({"CONVERSATION_TEXT": conversation})
    
    return response.content