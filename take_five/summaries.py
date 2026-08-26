import re
from datetime import date, datetime, timedelta
import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from take_five.messages import ContextBuilder
from take_five.repository import TOPIC_CATEGORIES
from take_five.utils import get_prompt, RESPONSE_FORMATS
from take_five.models import DIGEST_MODEL

logger = logging.getLogger(__name__)

DIGEST_PROMPT = get_prompt("t5_week_summary")
digest_llm = ChatAnthropic(model=DIGEST_MODEL, max_tokens=1024)

OUTER_DIGEST_PROMPT = get_prompt("t5_week_summary_outer")
outer_digest_llm = ChatAnthropic(model=DIGEST_MODEL, max_tokens=1024)


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


# ---------------------------------------------------------------------------
# Outer-circle digest (redacted) -- see 2026-08-26 design discussion.
# Reads inner-circle content but omits clinical/dignity-sensitive detail via
# an omission-instructed prompt, then checks its own output before it is
# ever considered postable. Never trust generation alone for something where
# being wrong has real consequences -- same instinct as prep-packet senior
# resolution skipping the LLM for its safety-critical routing step.
# ---------------------------------------------------------------------------

_DIGNITY_SENSITIVE_KEYWORDS = [
    'bowel', 'incontinen', 'diaper', 'toileting',
    'confused', 'confusion', 'dementia', 'alzheimer', 'cognitive decline',
    'depress', 'suicid', 'self-harm', 'self harm',
    'power of attorney', 'poa', 'estate', 'inheritance', 'will',
    'lawsuit', 'divorce', 'custody', 'affair',
]

# Keywords that are deliberately truncated word stems, meant to catch every
# inflected form (incontinence/incontinent, depressed/depression,
# suicide/suicidal) -- these get a leading word-boundary only, matching any
# suffix. Every other keyword gets word boundaries on BOTH sides, so it only
# matches as a complete standalone word/phrase, not as a substring inside an
# unrelated word.
#
# This distinction exists because of a real false positive found testing
# against Addams (2026-08-26): the naive substring check 'med ' in TEXT
# matched inside "Seemed in great spirits" (...see-MED in...), flagging and
# blocking a completely clean digest with zero clinical content. Naive
# substring containment is fine for TOPIC_CATEGORIES' original purpose
# (soft topic analytics, where an occasional false match is harmless) but
# not for a hard block gate, where a false positive silently discards a
# good digest.
_STEM_KEYWORDS = {'incontinen', 'depress', 'suicid'}


def _sanitize_keyword(kw: str) -> str:
    """Strip whitespace and a trailing period -- several TOPIC_CATEGORIES
    entries carry these as crude boundary markers (e.g. 'med ', 'dr.') from
    their original soft-matching use case; word-boundary regex makes that
    unnecessary and, in 'med ''s case, was actively causing the false
    positive above."""
    return kw.strip().rstrip('.')


def _build_restricted_pattern() -> re.Pattern:
    raw_keywords = (
        TOPIC_CATEGORIES['Medical & health']
        + TOPIC_CATEGORIES['Medications']
        + _DIGNITY_SENSITIVE_KEYWORDS
    )
    parts = []
    seen = set()
    for kw in raw_keywords:
        clean = _sanitize_keyword(kw)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        escaped = re.escape(clean)
        if clean in _STEM_KEYWORDS:
            parts.append(r'\b' + escaped)  # leading boundary only -- matches any suffix
        else:
            parts.append(r'\b' + escaped + r'\b')
    return re.compile('(' + '|'.join(parts) + ')', re.IGNORECASE)


_RESTRICTED_PATTERN = _build_restricted_pattern()

# Matches blood-pressure-shaped numbers ("137/82") and heart-rate-shaped
# numbers ("62 BPM", "62 beats per minute") -- vitals slip past keyword
# matching entirely since they're just digits, not words from either
# TOPIC_CATEGORIES list or _DIGNITY_SENSITIVE_KEYWORDS above.
_VITALS_RE = re.compile(
    r'\b\d{2,3}\s*/\s*\d{2,3}\b|\b\d{2,3}\s*(?:bpm|beats per minute)\b',
    re.IGNORECASE,
)


def _contains_restricted_content(text: str) -> list[str]:
    """
    Deterministic safety net over an already-generated outer-circle digest,
    checked before it is ever posted or treated as final. This is a second,
    independent layer behind t5_week_summary_outer.md's own omission
    instructions -- the prompt is the first line of defense, this is the
    one that actually blocks a bad output rather than just asking nicely.

    Reuses the existing TOPIC_CATEGORIES keyword lists from repository.py
    (Medical & health, Medications) as a base, extended with dignity-
    sensitive personal topics that aren't clinical in the drug/diagnosis
    sense but still don't belong in a wider-circle digest, plus a regex for
    vitals-shaped numbers that keyword matching alone won't catch.

    Matching is word-boundary anchored (see _build_restricted_pattern), not
    naive substring containment -- "Seemed" does not match "med", "willing"
    does not match "will", etc. Deliberate word stems (incontinen, depress,
    suicid) still match any suffix, since that's their intended purpose.

    Returns the list of matched terms/patterns found, exactly as they
    appeared in the text (empty list = clean). Callers should treat any
    non-empty return as "do not auto-post."

    Not exhaustive -- a determined or unusually-phrased message could still
    slip past keyword matching (e.g. a medication referred to only by an
    unlisted brand name). This catches the known, common cases; it is a
    safety net, not a guarantee. New medication names added to
    TOPIC_CATEGORIES for the topic-analysis feature automatically strengthen
    this check too, since both draw from the same list.
    """
    hits = []
    for m in _RESTRICTED_PATTERN.finditer(text):
        matched = m.group(0).lower()
        if matched not in hits:
            hits.append(matched)

    if _VITALS_RE.search(text) and 'vitals-pattern' not in hits:
        hits.append('vitals-pattern')

    return hits


def generate_outer_weekly_digest(
    circle_id: str,
    response_format: str = "markdown",
    start_date: datetime = None,
    end_date: datetime = None,
) -> dict:
    """
    Generates a digest for an OUTER circle -- e.g. Peggy, who visits about
    once a month to cook dinner and check in, and needs enough general
    context (who visited, how they're doing, life this week) to have a
    caring conversation, without any clinical or dignity-sensitive detail
    that belongs only to the inner care circle.

    Reads from the outer circle itself PLUS its parent inner circle (see
    ContextBuilder.create_for_outer_digest) -- the reverse of the normal
    boundary -- using t5_week_summary_outer.md, which is explicitly
    instructed on what to omit (medications, diagnoses, vitals, continence,
    cognitive/mental-health specifics, family/financial/legal matters) and
    what to always keep (that a visit happened, general mood, life content,
    new circle members). "What Needs Attention" is dropped entirely for
    outer circles; "Coming Up" is restricted to non-medical events.

    The generated text is then checked by _contains_restricted_content()
    before being considered final -- never trust the prompt alone for
    something where being wrong has real consequences.

    Returns {"digest": str, "blocked": bool, "flagged_terms": list[str]}.
    blocked=True means flagged_terms is non-empty and the digest must NOT
    be auto-posted -- callers should route to manual review instead. The
    digest text is still returned even when blocked, so a human reviewer
    can see exactly what tripped the check and decide what to do.
    """
    if start_date is None:
        start_date = datetime.now() - timedelta(days=7)
    if end_date is None:
        end_date = datetime.now() + timedelta(days=1)

    logger.info(f"Generating OUTER digest for circle_id={circle_id} from {start_date} to {end_date}")

    ctx = ContextBuilder.create_for_outer_digest(circle_id, start_date, end_date)
    messages = ctx.get_recent_messages()

    if "No messages found" in messages:
        return {
            "digest": "No messages found for this period — nothing to summarise.",
            "blocked": False,
            "flagged_terms": [],
        }

    prompt_text = OUTER_DIGEST_PROMPT.format(
        conversation_text=messages,
        roster_context=ctx.get_roster(),
        current_date=date.today().strftime("%A, %B %d, %Y"),
        calendar_context=_build_calendar_context(start_date, end_date),
        response_format=RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["markdown"]),
    )

    response = outer_digest_llm.invoke([HumanMessage(content=prompt_text)])
    digest_text = response.content if hasattr(response, "content") else str(response)

    flagged = _contains_restricted_content(digest_text)
    if flagged:
        logger.warning(
            f"[outer-digest] Restricted content detected for circle {circle_id}: {flagged}"
        )

    return {"digest": digest_text, "blocked": bool(flagged), "flagged_terms": flagged}
