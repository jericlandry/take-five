import os
import json
import logging
from datetime import datetime, date

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, ToolMessage

from take_five.repository import TakeFiveRepository
from take_five.memory import get_embedding
from take_five.utils import fetch_prompt, RESPONSE_FORMATS

logger = logging.getLogger(__name__)

llm_with_tools = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=1024)

# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

_tool_context: dict = {}

@tool
def save_clinical_record(
    person_id: str,
    resource_type: str,
    medication_name: str,
    dosage: str,
    instructions: str,
    is_supplement: bool,
    brand_name: str = None,
    form: str = None,
    prescriber: str = None,
    pharmacy: str = None,
    rx_number: str = None,
    refill_date: str = None,
    quantity: str = None,
    schedule: dict = None,
    notes: str = None,
    status: str = "active",
) -> str:
    """
    Save a confirmed medication or clinical record to the Take Five database.

    Call this tool when the user has explicitly confirmed the medication details
    are correct and wants to save them. Do not call it if there are still
    unresolved corrections or missing required fields.

    Args:
        person_id:       UUID of the care recipient this record belongs to.
                         Must be a senior in the circle — get this from the roster.
        resource_type:   FHIR resource type. Use 'MedicationStatement' for all
                         medications and supplements.
        medication_name: Full medication name as confirmed.
        dosage:          Dosage/strength (e.g. '5mg', '1000mg').
        instructions:    How and when to take it (e.g. 'Take 1 tablet at bedtime').
        is_supplement:   True if this is a vitamin/supplement, False if prescription.
        brand_name:      Brand name if different from medication_name.
        form:            Dosage form: tablet, capsule, liquid, etc.
        prescriber:      Prescribing doctor's name (required for prescriptions).
        pharmacy:        Pharmacy name.
        rx_number:       Prescription number.
        refill_date:     Refill date as string (YYYY-MM-DD if known).
        quantity:        Quantity on the label.
        schedule:        Dosing schedule as dict, e.g. {"morning": true, "evening": true}.
        notes:           Free-text family additions (preferences, context).
        status:          'active' | 'discontinued' | 'as_needed'. Default: 'active'.
    """
    repo         = _tool_context.get('repo')
    circle_id    = _tool_context.get('circle_id')
    confirmed_by = _tool_context.get('confirmed_by_person_id')

    if not repo or not circle_id:
        return json.dumps({"success": False, "error": "Missing tool context — circle_id or repo not set."})

    data = {
        "medication_name": medication_name,
        "brand_name":      brand_name,
        "dosage":          dosage,
        "form":            form,
        "instructions":    instructions,
        "is_supplement":   is_supplement,
        "prescriber":      prescriber,
        "pharmacy":        pharmacy,
        "rx_number":       rx_number,
        "refill_date":     refill_date,
        "quantity":        quantity,
        "schedule":        schedule or {},
    }
    data = {k: v for k, v in data.items() if v is not None}

    try:
        record = repo.save_clinical_record(
            circle_id=circle_id,
            person_id=person_id,
            resource_type=resource_type,
            data=data,
            notes=notes,
            status=status,
            confirmed_by=confirmed_by,
        )
        logger.info(
            f"[tools] Clinical record saved — "
            f"id: {record['id']}, type: {resource_type}, medication: {medication_name}"
        )
        return json.dumps({
            "success":       True,
            "record_id":     str(record['id']),
            "medication":    medication_name,
            "person_id":     person_id,
            "resource_type": resource_type,
        })
    except Exception as e:
        logger.error(f"[tools] save_clinical_record failed: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


TOOLS = [save_clinical_record]

# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

class ContextBuilder:
    def __init__(self, circle_id: str, question: str):
        self.repo = TakeFiveRepository()
        self.circle_id = circle_id
        self.question = question

    @classmethod
    async def create(cls, circle_id: str, question: str) -> "ContextBuilder":
        instance = cls(circle_id, question)
        embedding = await get_embedding(question, is_query=True)
        instance._roster         = instance._build_roster()
        instance._circle_context = instance._load_circle_context()
        instance._recent         = instance._build_recent_messages()
        instance._semantic       = instance._build_semantic(embedding)
        return instance

    @classmethod
    def create_for_digest(
        cls,
        circle_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> "ContextBuilder":
        instance = cls(circle_id, question="")
        instance._roster         = instance._build_roster()
        instance._circle_context = instance._load_circle_context()
        instance._recent         = instance._build_recent_messages(start_date, end_date)
        instance._semantic       = ""
        return instance

    def _build_roster(self) -> str:
        roster = self.repo.fetch_circle_roster(self.circle_id)
        return self._format_roster_context(roster)

    def _format_roster_context(self, rows: list) -> str:
        if not rows:
            return "## Care Circle\n_No members found._\n"

        circle_name = rows[0]["circle_name"]
        lines = [f"## Care Circle: {circle_name}\n"]

        by_role: dict[str, list] = {}
        for row in rows:
            by_role.setdefault(row["person_role"], []).append(row)

        role_order  = ["subject", "coordinator", "caregiver", "family", "member"]
        role_labels = {
            "subject":     "Subjects (people being cared for)",
            "coordinator": "Coordinators",
            "caregiver":   "Caregivers",
            "family":      "Family Members",
            "member":      "Members",
        }

        for role in role_order:
            if role not in by_role:
                continue
            lines.append(f"### {role_labels.get(role, role.title())}")
            for row in by_role[role]:
                aliases   = ", ".join(row["person_aliases"] or [])
                alias_str = f" (also known as: {aliases})" if aliases else ""
                lines.append(f"- **{row['member_name']}**{alias_str} [person_id: {row['id']}]")
                if row["person_notes"]:
                    lines.append(f"  - {row['person_notes']}")
            lines.append("")

        return "\n".join(lines)

    def _build_recent_messages(
        self,
        start_date: datetime = None,
        end_date: datetime = None
    ) -> str:
        recent_msgs = self.repo.get_messages(
            self.circle_id,
            start_date=start_date,
            end_date=end_date
        )
        return self._format_recent_messages_context(recent_msgs)

    def _format_recent_messages_context(self, rows: list) -> str:
        if not rows:
            return "## Recent Messages\n_No messages found._\n"

        today = date.today()
        lines = ["## Recent Messages (most recent first)\n"]
        for row in rows:
            if row["sent_at"]:
                ts = row["sent_at"].strftime("%A, %b %d, %Y %I:%M %p")
                days_ago = (today - row["sent_at"].date()).days
                if days_ago == 0:
                    recency = "today"
                elif days_ago == 1:
                    recency = "yesterday"
                else:
                    recency = f"{days_ago} days ago"
                ts_label = f"{ts} ({recency})"
            else:
                ts_label = "unknown time"

            sender = row["author_name"] or "Unknown"
            lines.append(f"- **{sender}** ({ts_label}): {row['body']}")

        return "\n".join(lines)

    def _build_semantic(self, embedding: list) -> str:
        chunks = self.repo.fetch_semantic_chunks(self.circle_id, embedding)
        return self._format_semantic_context(chunks)

    def _format_semantic_context(self, rows: list) -> str:
        if not rows:
            return "## Relevant Message History\n_No relevant messages found._\n"

        lines = ["## Relevant Message History (semantic search)\n"]
        for row in rows:
            header = row["context_header"] or "Unknown"
            lines.append(f"- **{header}**: {row['body']}")
            if row["context_summary"]:
                lines.append(f"  - _{row['context_summary']}_")

        return "\n".join(lines)

    def _load_circle_context(self) -> str:
        context_file = f"context/{self.circle_id}.md"
        if os.path.exists(context_file):
            with open(context_file, "r") as f:
                return f.read()
        return f"## Circle Context: {self.circle_id}\n\n_No context found._\n"

    def get_roster(self) -> str:          return self._roster
    def get_circle_context(self) -> str:  return self._circle_context
    def get_recent_messages(self) -> str: return self._recent
    def get_semantic(self) -> str:        return self._semantic


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Take Five, an AI care coordinator supporting a family caring for aging loved ones.
You answer questions clearly, accurately, and with warmth.

Guidelines:
- Use the person's preferred name or alias when appropriate
- For medication questions, be precise and complete — never guess
- For mood or behavioral questions, cite specific messages and dates where possible
- For clinical questions (e.g. dementia indicators), ground your answer in the messages
  and apply care domain knowledge — note patterns, changes over time, and flag anything
  worth raising with a doctor
- If the answer is not in the provided context, say so clearly rather than speculating
- Keep answers concise but complete

Tool use — save_clinical_record:
- A medication is PENDING when you see a message beginning with "💊 PENDING CONFIRMATION".
  This means the record has NOT been saved to the database yet — it is awaiting confirmation.
- A medication is SAVED only when the save_clinical_record tool has been called successfully
  in the current conversation and returned a success result. Never infer a record was saved
  from message history alone.
- Call save_clinical_record ONLY when the user explicitly confirms (e.g. "yes", "save it",
  "looks good", "that's right") AND there is a PENDING CONFIRMATION medication in context.
- Do NOT call it if the user is still making corrections or required fields are missing.
- The person_id must be the UUID of the care recipient (senior) from the roster —
  never use a family member's ID.
- If there is more than one senior in the circle and it is unclear which one the
  medication belongs to, ask before saving.
- After a successful save, confirm warmly and summarise what was recorded."""


def _build_human_message(
    today: str,
    circle_context: str,
    roster: str,
    recent_messages: str,
    semantic_chunks: str,
    response_format: str,
    question: str,
) -> str:
    """Mirrors the LangSmith t5-ask human message template exactly."""
    return f"""Today is {today}.
---
## Circle Context
{circle_context}
---
## Care Circle Roster
{roster}
---
## Recent Messages
{recent_messages}
---
## Relevant Message History
{semantic_chunks}
---
## Response Format
{response_format}
---
## Question
{question}"""


# ---------------------------------------------------------------------------
# ask_with_tools()
# ---------------------------------------------------------------------------

async def ask_with_tools(
    question: str,
    circle_id: str,
    response_format: str = "text",
    confirmed_by_person_id: str = None,
) -> str:
    global _tool_context

    repo = TakeFiveRepository()

    _tool_context = {
        'repo':                   repo,
        'circle_id':              circle_id,
        'confirmed_by_person_id': confirmed_by_person_id,
    }

    ctx = await ContextBuilder.create(circle_id, question)

    human_content = _build_human_message(
        today           = datetime.now().strftime("%B %d, %Y"),
        circle_context  = ctx.get_circle_context(),
        roster          = ctx.get_roster(),
        recent_messages = ctx.get_recent_messages(),
        semantic_chunks = ctx.get_semantic(),
        response_format = RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["text"]),
        question        = question,
    )

    user_message = HumanMessage(content=human_content)
    llm          = llm_with_tools.bind_tools(TOOLS)
    response     = llm.invoke([user_message], config={"system": SYSTEM_PROMPT})

    if response.tool_calls:
        tool_messages = []
        for tc in response.tool_calls:
            logger.info(f"[ask_with_tools] Tool call: {tc['name']} args: {tc['args']}")
            if tc['name'] == 'save_clinical_record':
                result = save_clinical_record.invoke(tc['args'])
                tool_messages.append(ToolMessage(content=result, tool_call_id=tc['id']))

        followup = llm.invoke(
            [user_message, response, *tool_messages],
            config={"system": SYSTEM_PROMPT}
        )
        return followup.content.strip()

    return response.content.strip()
