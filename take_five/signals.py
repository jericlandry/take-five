import json
import logging
import re
from typing import Optional

from anthropic import AsyncAnthropic

from take_five.repository import repo

logger = logging.getLogger(__name__)

DETECTION_PROMPT = """You are a clinical signal detector for Take Five, an AI care coordination platform for families supporting aging loved ones.

Your job is to read a single message from a family care circle and extract any health or safety observations about the people being cared for. You are not diagnosing — you are identifying observations worth tracking.

SUBJECTS IN THIS CARE CIRCLE:
{subjects}

SIGNAL TAXONOMY:
- safety: falls, injuries, emergencies, near-misses, fall risk
- functional: mobility, eating, sleep, cognition, dressing, energy, withdrawal from activities, hearing
- symptom: pain, nausea, dizziness, shortness of breath, physical complaints
- mood: affect, tearfulness, agitation, flat affect, good days, anxiety
- medication: refusals, missed doses, side effects, changes, new prescriptions

MENTION STYLE:
- direct: the sender is reporting their own firsthand observation — "I heard him coughing", "I saw her fall", "she wouldn't eat when I was there"
- oblique: the sender is relaying what someone else said or reported — "he said his neck hurts", "she told me she didn't sleep", "Jennifer mentioned Mom had fallen", "I heard from the aide that she fell"

CONFIDENCE:
Score 0.0-1.0. Lower for hedged language, third-party reports, ambiguous observations. Higher for direct firsthand statements of discrete events.

RULES:
- Extract ALL signals — a single message may contain many
- Only extract signals about the identified subjects, not family members or caregivers
- Capture signals even when phrased as questions, secondhand reports, or hedged observations — "what did the nurse say about dad's nipples?" is a symptom signal; "she seems a little off" is a mood signal
- Capture refusals of mobility aids (wheelchair, walker, cane) as functional/mobility signals
- Capture incontinence mentions, accidents, or protective bedding needs as functional signals
- Do not diagnose — report what was observed or mentioned
- Return [] ONLY for messages with zero health or safety content: pure logistics, technology troubleshooting, genealogy research, social banter, scheduling, book or TV discussion with no health context
- Do NOT return [] just because a signal is soft, oblique, secondhand, or mentioned in passing — those are exactly the signals worth capturing
- Do NOT return [] for messages that contain physical complaints, symptom mentions, mobility observations, medication notes, or behavioral changes — even minor ones
- If the same event or observation is mentioned more than once in a message, extract it only once — choose the most informative excerpt
- Keep raw_excerpt short — 10 to 15 words maximum, just enough to identify the signal. Do not quote full sentences.

WHAT NOT TO FLAG:
- Mood: Do not flag general positive affect or enjoyment — "good spirits", "enjoyed her meal", "having a great time", "all smiles" are NOT clinical signals. Only flag significant mood changes, emotional distress, agitation, tearfulness, or anxiety.
- Medication: Do not flag medication logistics — purchasing medications, filling pill trays, scheduling doses, confirming what was bought, or OTC purchases like pain relievers, supplements, or items bought at a pharmacy or grocery store. Only flag new prescriptions, discontinuations, refusals, missed doses, side effects, or compliance issues.
- Functional: Do not flag routine daily activities — reading, watching TV, eating meals, attending social events, going to church — as functional signals unless they represent a notable change from baseline or the person is visibly struggling. "She enjoyed her meatloaf" is not a signal. "She ran out of steam halfway through the outing" is a signal. Do not flag family coordination decisions about care — "we should get Lucy to help with dressing" or "we are adding more aide time" are logistics, not observations about the subject. Only flag direct observations of the subject struggling, declining, or changing.

CORROBORATION:
Set corroboration_suggested to true when:
- mention_style is oblique AND confidence is below 0.80
- the signal is secondhand or reported speech ("she said", "he mentioned", "I heard")
- the observation is ambiguous enough that confirmation from another circle member would meaningfully change how it should be interpreted

Set corroboration_suggested to false when:
- signal_category is safety OR signal_type contains fall, injury, fracture, emergency, 911, firemen, ambulance — never corroborate hard incidents regardless of mention style or confidence
- confidence is 0.85 or above with direct mention style
- the signal comes from a professional caregiver's firsthand observation
- the signal is a discrete, specific event (a fall happened, a medication was refused) even if reported secondhand — the event either happened or it didn't, corroboration won't change that

Return ONLY a valid JSON array. No preamble, no explanation, no markdown, no code fences. Do not wrap output in backticks of any kind. Do not reconsider or add commentary after the array. Raw JSON array only, nothing else. If no signals found, return [].

SCHEMA PER SIGNAL:
{
  "subject_name": string,
  "signal_category": "safety" | "functional" | "symptom" | "mood" | "medication",
  "signal_type": string,
  "raw_excerpt": string,
  "mention_style": "direct" | "oblique",
  "confidence": float,
  "corroboration_suggested": boolean
}"""


def _build_subjects_string(seniors: list) -> str:
    """Build the subjects string for the detection prompt from seniors list."""
    if not seniors:
        return "Unknown"
    parts = []
    for s in seniors:
        name = s["name"]
        aliases = s.get("aliases") or []
        if aliases:
            parts.append(f"{name} ({', '.join(aliases)})")
        else:
            parts.append(name)
    return ", ".join(parts)


def _resolve_subject_id(subject_name: str, seniors: list) -> Optional[str]:
    """Match the model's subject_name back to a person_id."""
    subject_lower = subject_name.lower()
    for senior in seniors:
        if subject_lower in senior["name"].lower():
            return str(senior["id"])
        aliases = senior.get("aliases") or []
        for alias in aliases:
            if subject_lower in alias.lower():
                return str(senior["id"])
    return None


def _strip_and_parse(raw: str) -> list:
    """Strip markdown fences, trailing commentary, then parse JSON."""
    # Strip code fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'\s*```$', '', raw).strip()
    # Strip anything after the first closing bracket
    first_end = raw.find("]")
    if first_end != -1:
        raw = raw[:first_end + 1].strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        # Try recovery — find last complete object
        last_close = raw.rfind("}")
        if last_close > 0:
            try:
                recovered = json.loads(raw[:last_close + 1] + "]")
                return recovered if isinstance(recovered, list) else []
            except json.JSONDecodeError:
                pass
        return []


async def detect_clinical_signals(
    message_id: str,
    circle_id: str,
    body: str,
    channel: str = "groupme",
) -> None:
    """
    Async signal detection agent. Runs post-message-storage.
    Detects clinical signals in a message and writes records to clinical_signals.
    Never raises — failures are logged and swallowed so the pipeline stays clean.
    """
    try:
        # Fetch seniors for this circle to build subjects string and resolve IDs
        seniors = repo.get_seniors_in_circle(circle_id)
        if not seniors:
            logger.info(f"[signals] No seniors in circle {circle_id} — skipping detection")
            return

        subjects_str = _build_subjects_string(seniors)
        prompt = DETECTION_PROMPT.replace("{subjects}", subjects_str)

        client = AsyncAnthropic()
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": prompt + "\n\n---\n\nMESSAGE TO ANALYZE:\n" + body
            }]
        )

        raw = response.content[0].text.strip()
        signals = _strip_and_parse(raw)

        if not signals:
            logger.info(f"[signals] No signals detected in message {message_id}")
            return

        logger.info(f"[signals] {len(signals)} signal(s) detected in message {message_id}")

        for signal in signals:
            # Skip malformed signal objects
            if not isinstance(signal, dict):
                continue
            if not signal.get("signal_category") or not signal.get("signal_type"):
                continue

            subject_id = _resolve_subject_id(
                signal.get("subject_name", ""),
                seniors
            )

            repo.save_clinical_signal(
                message_id=message_id,
                circle_id=circle_id,
                subject_id=subject_id,
                signal_category=signal["signal_category"],
                signal_type=signal["signal_type"],
                raw_excerpt=signal.get("raw_excerpt"),
                mention_style=signal.get("mention_style"),
                confidence=signal.get("confidence"),
                channel=channel,
                corroboration_suggested=signal.get("corroboration_suggested", False),
            )

    except Exception as e:
        logger.error(f"[signals] Detection failed for message {message_id}: {e}", exc_info=True)
