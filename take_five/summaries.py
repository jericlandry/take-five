import json
import re
from datetime import date, datetime, timedelta
import logging
from typing import Dict, List, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

from take_five.engagement.life_log import extract_life_log_topic
from take_five.messages import ContextBuilder
from take_five.repository import repo, TOPIC_CATEGORIES
from take_five.utils import get_prompt, RESPONSE_FORMATS, build_calendar_context
from take_five.models import DIGEST_MODEL, PRIVACY_REVIEW_MODEL

logger = logging.getLogger(__name__)

DIGEST_PROMPT = get_prompt("t5_week_summary")
digest_llm = ChatAnthropic(model=DIGEST_MODEL, max_tokens=1024)

OUTER_DIGEST_PROMPT = get_prompt("t5_week_summary_outer")
outer_digest_llm = ChatAnthropic(model=DIGEST_MODEL, max_tokens=1024)

# Single source of truth for what the outer circle may not see -- injected
# into BOTH the outer digest prompt and the privacy reviewer prompt, so the
# generator and the reviewer can never drift apart on the rules.
OUTER_PRIVACY_RULES = get_prompt("t5_outer_privacy_rules")
PRIVACY_REVIEW_PROMPT = get_prompt("t5_outer_privacy_review")
# 4096, not 2048: the reviewer echoes the whole digest back plus a changes
# list, and a truncated reply loses the closing </revised_digest> tag.
privacy_review_llm = ChatAnthropic(model=PRIVACY_REVIEW_MODEL, max_tokens=4096)

SENIOR_DIGEST_PROMPT = get_prompt("t5_week_summary_senior")
# Shorter max_tokens than the family digest -- this is meant to read like a
# short personal note, not a report; a low ceiling also discourages the
# model from padding it out into something digest-shaped.
senior_digest_llm = ChatAnthropic(model=DIGEST_MODEL, max_tokens=400)


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
        calendar_context=build_calendar_context(start_date, end_date),
        care_calendar=ctx.get_calendar(),
        response_format=RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["markdown"]),
    )

    response = digest_llm.invoke([HumanMessage(content=prompt_text)])

    return response.content if hasattr(response, "content") else str(response)


# ---------------------------------------------------------------------------
# Senior-facing weekly email -- see chat history (2026-09-xx) for the design
# reasoning. Deliberately built from a NARROWER, safer set of sources than
# generate_weekly_digest(): never the raw inner-circle conversation, only
# extract_life_log_topic() (the same grounded, never-generic extraction the
# Tuesday engagement cron uses) and upcoming prep-packet appointments. Both
# of those sources are safe by construction -- there is structurally nothing
# clinical/concern-toned in the context window for the model to leak -- 
# rather than relying on a prompt instruction to correctly omit sensitive
# content out of the full conversation history every single week.
# ---------------------------------------------------------------------------

def _format_life_log_section(topic: Optional[Dict]) -> str:
    if not topic:
        return "(Nothing grounded came up this week -- skip this part of the email entirely.)"
    subject = topic.get("subject_name")
    excerpt = topic["excerpt"]
    return f"About {subject}: {excerpt}" if subject else excerpt


def _format_upcoming_events_section(events: List[Dict]) -> str:
    if not events:
        return "(Nothing scheduled this week -- skip this part of the email entirely.)"
    lines = []
    for e in events:
        appt_date = e["appointment_date"].strftime("%A, %B %d")
        doctor = e.get("doctor_name") or "a doctor's appointment"
        desc = e.get("appointment_desc")
        label = f"{doctor}" + (f" -- {desc}" if desc else "")
        lines.append(f"- {appt_date}: {label}")
    return "\n".join(lines)


def _format_invitation_target(senior_name: str, other_seniors: List[Dict]) -> str:
    """
    Grammar handled here in Python (singular "is" vs plural "are") rather
    than left to the model to get right -- same reasoning as
    _build_calendar_context computing dates itself instead of asking the
    model to do weekday arithmetic.
    """
    if other_seniors:
        others = " and ".join(s["name"] for s in other_seniors)
        return f"how {senior_name} and {others} are doing this week"
    return f"how {senior_name} is doing this week"


def _format_reply_footer(other_seniors: List[Dict]) -> str:
    """
    Static instructional line, always exactly the same wording every week --
    deliberately NOT part of the LLM's job (unlike the warm invitation in
    the prompt itself, which is meant to vary). This is teaching a
    mechanic (hit reply), not making conversation, so consistency matters
    more than warmth here -- same reasoning as never asking the model to
    reproduce the reply-to address itself.
    """
    who = "from you both" if other_seniors else "from you"
    return f"Just reply to this email with any updates. I'd love to hear {who}."


async def generate_senior_digest(circle_id: str, senior: Dict, other_seniors: List[Dict]) -> Optional[str]:
    """
    Generate the senior-facing weekly email body for one senior in a circle.
    other_seniors: any other role='senior' people in the same circle (e.g. a
    spouse sharing the circle) -- included only to phrase the closing
    invitation ("how you and Mom are doing"), not fed any additional data.

    async because extract_life_log_topic() is (unlike the rest of this
    file's sync ChatAnthropic calls) -- caller (main_summary.py) wraps this
    with asyncio.run().

    Returns None on LLM failure; caller skips sending rather than emailing
    a broken/empty note.
    """
    topic = await extract_life_log_topic(circle_id)
    upcoming = repo.get_upcoming_prep_packets(circle_id)

    prompt_text = SENIOR_DIGEST_PROMPT.format(
        current_date=date.today().strftime("%A, %B %d, %Y"),
        senior_name=senior["name"],
        invitation_target=_format_invitation_target(senior["name"], other_seniors),
        life_log_section=_format_life_log_section(topic),
        upcoming_events_section=_format_upcoming_events_section(upcoming),
    )

    try:
        response = senior_digest_llm.invoke([HumanMessage(content=prompt_text)])
        body = response.content if hasattr(response, "content") else str(response)
        return f"{body}\n\n{_format_reply_footer(other_seniors)}"
    except Exception as e:
        logger.error(
            f"[senior-digest] Generation failed for circle_id={circle_id}, "
            f"senior={senior.get('name')}: {e}"
        )
        return None

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

# Two-tier gate (2026-10-15). The Landry F&F digest was blocked on
# 2026-10-11 for "dentist appointment" and, on replay, also for "appreciate
# hearing" -- both harmless. These are everyday words that are only
# SOMETIMES sensitive, so they are left to review_outer_digest()'s
# sentence-level judgment and excluded from the hard gate. Everything else
# in the keyword lists (medication names, diagnoses, dementia, continence,
# vitals pattern, etc.) still hard-blocks regardless of what the reviewer
# did -- the reviewer is the same kind of model as the generator, so it
# reduces leaks but is not trusted alone. TOPIC_CATEGORIES is untouched;
# these words are still correct for topic analytics.
# Values are post-_sanitize_keyword (e.g. 'dr.' -> 'dr').
_GATE_SOFT_TERMS = {
    # Medical & health -- logistics or ordinary-English meanings
    'appointment', 'appt', 'doctor', 'dr', 'nurse', 'hospital', 'memory',
    'hearing', 'audiologist', 'sleep', 'weight', 'test', 'decline',
    'assisted living', 'physical therapy', 'therapist', 'geriatric',
    'fall', 'unsteady', 'wheelchair', 'walker',
    # Medications -- errands and non-drug meanings
    'pharmacy', 'tablet', 'supplement', 'vitamin',
    # Dignity list -- common English words
    'will', 'estate', 'confused', 'confusion',
}


def _sanitize_keyword(kw: str) -> str:
    """Strip whitespace and a trailing period -- several TOPIC_CATEGORIES
    entries carry these as crude boundary markers (e.g. 'med ', 'dr.') from
    their original soft-matching use case; word-boundary regex makes that
    unnecessary and, in 'med ''s case, was actively causing the false
    positive above."""
    return kw.strip().rstrip('.')


def _build_restricted_pattern(exclude_soft: bool = True) -> re.Pattern:
    raw_keywords = (
        TOPIC_CATEGORIES['Medical & health']
        + TOPIC_CATEGORIES['Medications']
        + _DIGNITY_SENSITIVE_KEYWORDS
    )
    parts = []
    seen = set()
    for kw in raw_keywords:
        clean = _sanitize_keyword(kw)
        if not clean or clean in seen or (exclude_soft and clean in _GATE_SOFT_TERMS):
            continue
        seen.add(clean)
        escaped = re.escape(clean)
        if clean in _STEM_KEYWORDS:
            parts.append(r'\b' + escaped)  # leading boundary only -- matches any suffix
        else:
            parts.append(r'\b' + escaped + r'\b')
    return re.compile('(' + '|'.join(parts) + ')', re.IGNORECASE)


_RESTRICTED_PATTERN = _build_restricted_pattern()
# Hard + soft terms -- used only when the privacy review failed, so a
# digest nobody judged sentence-by-sentence gets the original strict gate.
_RESTRICTED_PATTERN_STRICT = _build_restricted_pattern(exclude_soft=False)

# Matches blood-pressure-shaped numbers ("137/82") and heart-rate-shaped
# numbers ("62 BPM", "62 beats per minute") -- vitals slip past keyword
# matching entirely since they're just digits, not words from either
# TOPIC_CATEGORIES list or _DIGNITY_SENSITIVE_KEYWORDS above.
_VITALS_RE = re.compile(
    r'\b\d{2,3}\s*/\s*\d{2,3}\b|\b\d{2,3}\s*(?:bpm|beats per minute)\b',
    re.IGNORECASE,
)


def _contains_restricted_content(text: str, strict: bool = False) -> list[str]:
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
    pattern = _RESTRICTED_PATTERN_STRICT if strict else _RESTRICTED_PATTERN
    for m in pattern.finditer(text):
        matched = m.group(0).lower()
        if matched not in hits:
            hits.append(matched)

    if _VITALS_RE.search(text) and 'vitals-pattern' not in hits:
        hits.append('vitals-pattern')

    return hits


def _response_text(response) -> str:
    """
    Plain text from a ChatAnthropic response. Newer models can return
    .content as a LIST of content blocks rather than a string; str() on that
    list yields its Python repr (literal \\n, escaped quotes), which is what
    broke the first reviewer test on 2026-10-15. Join only the text blocks.
    """
    content = response.content if hasattr(response, "content") else response
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


_REVISED_RE = re.compile(r"<revised_digest>\s*(.*?)\s*</revised_digest>", re.DOTALL)
_CHANGES_RE = re.compile(r"<changes>\s*(.*?)\s*</changes>", re.DOTALL)


def review_outer_digest(draft: str) -> Optional[dict]:
    """
    Privacy review of a generated outer-circle digest: one structured LLM
    call (not a tool-using agent) that rewrites anything private and leaves
    everything else untouched. Sees ONLY the draft and the rules -- never
    the inner-circle conversation -- so it can remove or soften content but
    has no source to add anything new from.

    Returns {"revised_digest": str, "changes": list[dict]}, or None if the
    call or parsing failed. On None the caller falls back to running the
    hard gate on the unreviewed draft (fail closed, not open).
    """
    prompt_text = PRIVACY_REVIEW_PROMPT.format(privacy_rules=OUTER_PRIVACY_RULES, digest=draft)
    try:
        response = privacy_review_llm.invoke([HumanMessage(content=prompt_text)])
    except Exception as e:
        logger.error(f"[outer-review] Review call failed: {e}")
        return None
    text = _response_text(response)

    m = _REVISED_RE.search(text)
    if not m or not m.group(1).strip():
        stop = (getattr(response, "response_metadata", None) or {}).get("stop_reason")
        logger.error(
            f"[outer-review] No <revised_digest> block in reviewer output "
            f"(stop_reason={stop}, {len(text)} chars). Start: {text[:300]!r}"
        )
        return None

    changes: list = []
    c = _CHANGES_RE.search(text)
    if c:
        try:
            parsed = json.loads(c.group(1))
            if isinstance(parsed, list):
                changes = parsed
        except ValueError:
            logger.warning("[outer-review] Could not parse <changes> JSON -- keeping revised digest, changes unrecorded")

    return {"revised_digest": m.group(1).strip(), "changes": changes}


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
        privacy_rules=OUTER_PRIVACY_RULES,
        conversation_text=messages,
        roster_context=ctx.get_roster(),
        current_date=date.today().strftime("%A, %B %d, %Y"),
        calendar_context=build_calendar_context(start_date, end_date),
        response_format=RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["markdown"]),
    )

    response = outer_digest_llm.invoke([HumanMessage(content=prompt_text)])
    draft = response.content if hasattr(response, "content") else str(response)

    review = review_outer_digest(draft)
    if review:
        digest_text = review["revised_digest"]
        changes = review["changes"]
        for ch in changes:
            logger.info(f"[outer-review] circle {circle_id}: {ch}")
    else:
        digest_text = draft
        changes = []
        logger.warning(
            f"[outer-review] Review failed for circle {circle_id} -- "
            f"strict gate (hard + soft terms) will run on the unreviewed draft"
        )

    flagged = _contains_restricted_content(digest_text, strict=review is None)
    if flagged:
        logger.warning(
            f"[outer-digest] Restricted content detected for circle {circle_id}: {flagged}"
        )

    return {
        "digest": digest_text,
        "draft": draft,
        "reviewed": review is not None,
        "changes": changes,
        "blocked": bool(flagged),
        "flagged_terms": flagged,
    }
