import os
from datetime import date, datetime, timedelta
import logging

from langchain_anthropic import ChatAnthropic

from take_five.messages import ContextBuilder
from take_five.utils import fetch_prompt, RESPONSE_FORMATS

SUMMARY_PROMPT_NAME = os.getenv("LANGSMITH_PROMPT_NAME", "t5-week-summary")

logging.basicConfig(level=logging.INFO)

chain = fetch_prompt(SUMMARY_PROMPT_NAME) | ChatAnthropic(model="claude-sonnet-4-6", max_tokens=1024)


def generate_weekly_digest(
    circle_id: str,
    response_format: str = "markdown",
    start_date: datetime = None,
    end_date: datetime = None,
) -> str:

    if start_date is None:
        start_date = datetime.now() - timedelta(days=7)
    if end_date is None:
        end_date = datetime.now() + timedelta(days=1)

    logging.info(f"Generating digest for circle_id={circle_id} from {start_date} to {end_date}")

    ctx = ContextBuilder.create_for_digest(circle_id, start_date, end_date)
    messages = ctx.get_recent_messages()

    if "No messages found" in messages:
        return "No messages found for this period — nothing to summarise."

    response = chain.invoke({
        "conversation_text": messages,
        "roster_context":    ctx.get_roster(),
        "current_date":      date.today().strftime("%A, %B %d, %Y"),
        "response_format":   RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["markdown"])
    })

    return response.content if hasattr(response, "content") else str(response)