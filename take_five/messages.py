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
            person_id=person_id,
            resource_type=resource_type,
            data=data,
            notes=notes,
            status=status,
            confirmed_by=confirmed_by,
            circle_id=circle_id,  # provenance — which chat it came from
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


@tool
def patch_clinical_record(
    record_id: str,
    event_type: str,
    updated_fields: dict = None,
    notes: str = None,
) -> str:
    """
    Update an existing clinical record and log the event to the audit trail.

    Use this tool — never save_clinical_record — when a medication or other
    clinical record already exists in Clinical Records and the user is:
    - Correcting a field (event_type='updated'): pass updated_fields with only
      what changed, e.g. {"dosage": "10mg"} or {"instructions": "take twice daily"}.
    - Reporting a refill (event_type='refilled'): pass no updated_fields.
      The record is unchanged — the event is the signal.
    - Discontinuing (event_type='discontinued'): pass no updated_fields.
      The record status will be set to 'discontinued'.

    Args:
        record_id:      UUID of the existing clinical_records row to update.
                        Get this from the [record_id: ...] shown in Clinical Records.
        event_type:     'updated' | 'refilled' | 'discontinued'
        updated_fields: Dict of only the fields being changed (for 'updated' only).
                        Keys must match the field names in the record's data JSONB,
                        e.g. {"dosage": "10mg"} or {"prescriber": "Dr. Smith"}.
        notes:          Optional free-text note to attach to the event.
    """
    repo         = _tool_context.get('repo')
    confirmed_by = _tool_context.get('confirmed_by_person_id')

    if not repo:
        return json.dumps({"success": False, "error": "Missing tool context — repo not set."})

    try:
        record = repo.patch_clinical_record(
            record_id=record_id,
            event_type=event_type,
            updated_fields=updated_fields,
            notes=notes,
            confirmed_by=confirmed_by,
        )
        data = record['data'] if isinstance(record['data'], dict) else json.loads(record['data'])
        logger.info(
            f"[tools] Clinical record patched — "
            f"id: {record_id}, event_type: {event_type}, "
            f"medication: {data.get('medication_name', 'unknown')}"
        )
        return json.dumps({
            "success":    True,
            "record_id":  str(record['id']),
            "event_type": event_type,
            "medication": data.get('medication_name'),
        })
    except Exception as e:
        logger.error(f"[tools] patch_clinical_record failed: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


TOOLS = [save_clinical_record, patch_clinical_record]

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
        instance._roster          = instance._build_roster()
        instance._circle_context  = instance._load_circle_context()
        instance._clinical        = instance._build_clinical_records()
        instance._recent          = instance._build_recent_messages()
        instance._semantic        = instance._build_semantic(embedding)
        return instance

    @classmethod
    def create_for_digest(
        cls,
        circle_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> "ContextBuilder":
        instance = cls(circle_id, question="")
        instance._roster          = instance._build_roster()
        instance._circle_context  = instance._load_circle_context()
        instance._clinical        = instance._build_clinical_records()
        instance._recent          = instance._build_recent_messages(start_date, end_date)
        instance._semantic        = ""
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

        role_order = ["senior", "subject", "coordinator", "caregiver", "family", "member"]
        role_labels = {
            "senior":      "Seniors (care recipients)",
            "subject":     "Subjects (care recipients)",
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

        for role, members in by_role.items():
            if role in role_order:
                continue
            lines.append(f"### {role.title()}")
            for row in members:
                aliases   = ", ".join(row["person_aliases"] or [])
                alias_str = f" (also known as: {aliases})" if aliases else ""
                lines.append(f"- **{row['member_name']}**{alias_str} [person_id: {row['id']}]")
                if row["person_notes"]:
                    lines.append(f"  - {row['person_notes']}")
            lines.append("")

        return "\n".join(lines)

    def _build_clinical_records(self) -> str:
        records = self.repo.get_clinical_records_for_circle(self.circle_id)
        if not records:
            return "## Clinical Records\n_No clinical records on file._\n"

        lines = ["## Clinical Records\n"]

        # Group by person
        by_person: dict[str, list] = {}
        person_names: dict[str, str] = {}
        for r in records:
            pid = str(r['person_id'])
            by_person.setdefault(pid, []).append(r)
            person_names[pid] = r['person_name']

        for person_id, person_records in by_person.items():
            person_name = person_names[person_id]
            lines.append(f"### {person_name}")

            by_type: dict[str, list] = {}
            for r in person_records:
                by_type.setdefault(r['resource_type'], []).append(r)

            for resource_type, type_records in by_type.items():
                label = {
                    "MedicationStatement": "Medications",
                    "Condition":           "Conditions / Diagnoses",
                    "Observation":         "Observations",
                    "Appointment":         "Appointments",
                    "AllergyIntolerance":  "Allergies",
                    "Procedure":           "Procedures",
                    "CareTeamMember":      "Care Team",
                }.get(resource_type, resource_type)

                lines.append(f"\n**{label}**")

                for rec in type_records:
                    data      = rec['data'] if isinstance(rec['data'], dict) else json.loads(rec['data'])
                    record_id = str(rec['id'])
                    status    = rec['status']

                    if resource_type == 'CareTeamMember':
                        name    = data.get('name', 'Unknown')
                        cred    = data.get('credential', '')
                        spec    = data.get('specialty', '')
                        role    = data.get('role', '')
                        phone   = data.get('phone', '')
                        primary = f"- **{name}**"
                        if cred:  primary += f" {cred}"
                        if spec:  primary += f" \u2014 {spec}"
                        if role:  primary += f" ({role})"
                        lines.append(primary)
                        if phone: lines.append(f"  Phone: {phone}")
                        lines.append(f"  [record_id: {record_id}]")
                        continue

                    name  = data.get('medication_name') or data.get('condition') or data.get('symptom') or 'Unknown'
                    dose  = data.get('dosage', '')
                    instr = data.get('instructions', '')
                    notes = rec.get('notes') or ''

                    primary = f"- **{name}**"
                    if dose:                          primary += f" ({dose})"
                    if status and status != 'active': primary += f" \u2014 _{status}_"
                    lines.append(primary)

                    if instr: lines.append(f"  Instructions: {instr}")

                    detail_fields = [
                        ('prescriber',  'Prescriber'),
                        ('pharmacy',    'Pharmacy'),
                        ('refill_date', 'Refill date'),
                        ('quantity',    'Quantity'),
                        ('rx_number',   'Rx#'),
                        ('form',        'Form'),
                    ]
                    details = [f"{lbl}: {data[field]}" for field, lbl in detail_fields if data.get(field)]
                    if details: lines.append(f"  {' | '.join(details)}")
                    if notes:   lines.append(f"  Note: {notes}")
                    lines.append(f"  [record_id: {record_id}]")

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

    def get_roster(self) -> str:           return self._roster
    def get_circle_context(self) -> str:   return self._circle_context
    def get_clinical_records(self) -> str: return self._clinical
    def get_recent_messages(self) -> str:  return self._recent
    def get_semantic(self) -> str:         return self._semantic


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Take Five, an AI care coordinator supporting a family caring for aging loved ones.
You answer questions clearly, accurately, and with warmth.

Guidelines:
- Use the person's preferred name or alias when appropriate
- For medication questions, be precise and complete — never guess
- Always use the Clinical Records section as the authoritative source for medications,
  diagnoses, and other clinical facts. Message history is supplementary context only.
- For mood or behavioral questions, cite specific messages and dates where possible
- For clinical questions (e.g. dementia indicators), ground your answer in the messages
  and apply care domain knowledge — note patterns, changes over time, and flag anything
  worth raising with a doctor
- If the answer is not in the provided context, say so clearly rather than speculating
- Keep answers concise but complete

Pre-visit summary:
When the user asks for a pre-visit summary or appointment preparation, follow this process:

1. IDENTIFY THE DOCTOR — Look for the doctor's name in the request. Search the Clinical
   Records section for a matching CareTeamMember record. If found, note their specialty —
   this shapes what is clinically relevant. If not found, proceed without specialty context
   and note the doctor was not found in the care team records.

2. DETERMINE THE LOOKBACK WINDOW — Search the Recent Messages for any mention of a
   previous visit with this doctor (phrases like "appointment", "saw Dr.", "visit with",
   "follow-up", the doctor's name near a date). If you find a plausible last visit date,
   tell the user what you found and ask: "I found a mention of a visit with [doctor] on
   [date] — should I use that as the starting point?" Wait for confirmation. If you find
   nothing, ask: "When was your last appointment with [doctor]?" Once you have a confirmed
   start date, note it as the lookback window.

3. ASSEMBLE THE SUMMARY — Using the confirmed lookback window, produce a structured
   pre-visit summary with these sections:

   **Upcoming visit** — Doctor name, specialty, date/time if mentioned

   **Current medications** — Full list from Clinical Records. Flag any changes since last
   visit if discernible from messages (new meds added, anything discontinued).

   **Recent patterns** — What caregivers and family have reported since the last visit.
   Weight observations by the doctor's specialty: for psychiatry, emphasize mood, sleep,
   anxiety, diet, and medication compliance. For cardiology, emphasize chest symptoms,
   activity levels, swelling, and cardiac risk factors. For primary care, cover broadly.
   Cite specific messages and dates where possible.

   **Concerns to raise** — Things mentioned in the chat that warrant the doctor's
   attention. Infer from message content — flag anything that sounds like a symptom,
   a change in condition, or something a family member flagged as worrying. Note the
   source (who said it, when).

   **Questions to consider** — 2-3 questions the family might want to ask, generated
   from the patterns and concerns you identified. Frame as suggestions, not directives.

   Keep the tone warm and practical — this is for a family member to bring to the
   appointment, not a clinical document.

Tool use — save_clinical_record and patch_clinical_record:

SAVED vs PENDING:
- A medication is PENDING when you see a message beginning with "💊 PENDING CONFIRMATION".
  This means the record has NOT been saved to the database yet — it is awaiting confirmation.
- A medication is SAVED only when you see a message beginning with "[SAVED: record_id=...]"
  or "[PATCHED: record_id=...]" in the conversation history. These markers are written by
  the system only when the database write actually succeeded. Never infer a record was
  saved from any other message.

Choosing the right tool:
- Before calling save_clinical_record, check Clinical Records for a medication with the
  same or similar name for the same senior.
- If a matching record EXISTS → use patch_clinical_record, not save_clinical_record.
- If NO matching record exists → use save_clinical_record.

When a medication image or text update matches an existing record, ask:
  "I already have [name] on file for [senior]. Is this a refill, a correction to the
  existing record, or a different medication?"
Wait for the answer before calling any tool. Map the answer to event_type:
  - refill → event_type='refilled'
  - correction → event_type='updated', pass only the changed fields in updated_fields
  - different medication → call save_clinical_record as a new record

Call save_clinical_record when:
  (a) The user explicitly confirms a PENDING CONFIRMATION medication and no matching
      record exists, OR
  (b) The user directly provides medication details for a new medication
      (e.g. "add a medication for Mel", "save this prescription", "please add Metformin 500mg").

Call patch_clinical_record when:
  (a) The user confirms a PENDING CONFIRMATION and a matching record already exists, OR
  (b) The user provides a correction to an existing record
      (e.g. "actually the dose is 10mg", "update the instructions for Dayvigo"), OR
  (c) The user reports a refill of an existing medication, OR
  (d) The user says a medication has been discontinued.

General rules:
- Do NOT call either tool if required fields are missing without asking for them first.
- The person_id must be the UUID of the care recipient (senior) from the roster —
  never use a family member's ID.
- If there is more than one senior in the circle and it is unclear which one the
  medication belongs to, ask before saving.
- After a successful save, confirm warmly and summarise what was recorded.
- After a successful patch, confirm warmly with the event type:
  refilled → "Refill logged for [name]."
  updated  → "Updated [field] for [name] from [old] to [new]."
  discontinued → "[name] has been marked as discontinued."
"""


def _build_human_message(
    today: str,
    circle_context: str,
    roster: str,
    clinical_records: str,
    recent_messages: str,
    semantic_chunks: str,
    response_format: str,
    question: str,
) -> str:
    return f"""Today is {today}.
---
## Circle Context
{circle_context}
---
## Care Circle Roster
{roster}
---
{clinical_records}
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
        today            = datetime.now().strftime("%B %d, %Y"),
        circle_context   = ctx.get_circle_context(),
        roster           = ctx.get_roster(),
        clinical_records = ctx.get_clinical_records(),
        recent_messages  = ctx.get_recent_messages(),
        semantic_chunks  = ctx.get_semantic(),
        response_format  = RESPONSE_FORMATS.get(response_format, RESPONSE_FORMATS["text"]),
        question         = question,
    )

    user_message = HumanMessage(content=human_content)
    llm          = llm_with_tools.bind_tools(TOOLS)
    response     = llm.invoke([user_message], config={"system": SYSTEM_PROMPT})

    if response.tool_calls:
        tool_messages      = []
        saved_record_ids   = []
        patched_record_ids = []

        for tc in response.tool_calls:
            logger.info(f"[ask_with_tools] Tool call: {tc['name']} args: {tc['args']}")
            if tc['name'] == 'save_clinical_record':
                result = save_clinical_record.invoke(tc['args'])
                parsed = json.loads(result)
                if parsed.get('success'):
                    saved_record_ids.append(parsed['record_id'])
                tool_messages.append(ToolMessage(content=result, tool_call_id=tc['id']))
            elif tc['name'] == 'patch_clinical_record':
                result = patch_clinical_record.invoke(tc['args'])
                parsed = json.loads(result)
                if parsed.get('success'):
                    patched_record_ids.append((parsed['record_id'], parsed.get('event_type', '')))
                tool_messages.append(ToolMessage(content=result, tool_call_id=tc['id']))

        followup = llm.invoke(
            [user_message, response, *tool_messages],
            config={"system": SYSTEM_PROMPT}
        )
        reply = followup.content.strip()

        # Prepend sentinel for state tracking — stripped before posting to GroupMe
        if saved_record_ids:
            record_ids = ", ".join(saved_record_ids)
            reply = f"[SAVED: record_id={record_ids}]\n{reply}"

        if patched_record_ids:
            markers = ", ".join(f"{rid}:{etype}" for rid, etype in patched_record_ids)
            reply = f"[PATCHED: record_id={markers}]\n{reply}"

        return reply

    return response.content.strip()
