import json
import logging
import re
from typing import Optional

from anthropic import AsyncAnthropic

from take_five.repository import repo
from take_five.utils import get_prompt

logger = logging.getLogger(__name__)

DETECTION_PROMPT = get_prompt("detection_prompt")


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
                request_corroboration=signal.get("corroboration_suggested", False),
            )

    except Exception as e:
        logger.error(f"[signals] Detection failed for message {message_id}: {e}", exc_info=True)
