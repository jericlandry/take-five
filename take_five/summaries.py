from datetime import date, datetime, timedelta
import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from take_five.messages import ContextBuilder
from take_five.utils import get_prompt, RESPONSE_FORMATS
from take_five.models import DIGEST_MODEL

logger = logging.getLogger(__name__)

DIGEST_PROMPT = get_prompt("t5_week_summary")
digest_llm = ChatAnthropic(model=DIGEST_MODEL, max_tokens=1024)


def _build_calendar_context(start_date: datetime, end_date: datetime, lookback_days: int = 3) -> str:
    """
    Deterministic day-name -> date lookup table for the digest window, so
    the model can resolve a relative day mentioned in a message ("we did X
    Friday") by lookup instead of computing weekday arithmetic itself --
    found doing this arithmetic wrong in production (labeled a message's
    "Friday" reference as August 22, 2026, which is actually a Saturday --
    see Kathy Landry / Landry F&F digest, 2026-08-26).

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

    logger.info(f"Generating digest for circle_id={circle_id} from {start_date} to {end_date}")

    ctx = ContextBuilder.create_for_digest(circle_id, start_date, end_date)
    messages = ctx.get_recent_messages()

    if "No messages found" in messages:
        return "No messages found for this period — nothing to summarise."

    prompt_text = DIGEST_PROMPT.format(
        conversation_text=messages,
        roster_context=ctx.get_roster(),
        current_date=date.today().strftime("%A, %B %d, %Y"),
        calendar_context=_build_calendar_context(start_date, end_date),
        response_format=RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["markdown"]),
    )

    response = digest_llm.invoke([HumanMessage(content=prompt_text)])

    return response.content if hasattr(response, "content") else str(response)
