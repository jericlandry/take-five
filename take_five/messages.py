import os
from datetime import datetime

from langchain_anthropic import ChatAnthropic
 
from take_five.repository import TakeFiveRepository
from take_five.memory import get_embedding
from take_five.utils import fetch_prompt

ANTHROPIC_MODEL       = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
conversation_chain = fetch_prompt("t5-ask") | ChatAnthropic(model="claude-sonnet-4-6", max_tokens=1024)

class ContextBuilder:
    def __init__(self, circle_id: str, question: str):
        self.repo = TakeFiveRepository()
        self.circle_id = circle_id
        self.question = question

    @classmethod
    async def create(cls, circle_id: str, question: str) -> "ContextBuilder":
        instance = cls(circle_id, question)
        embedding = await get_embedding(question, is_query=True)
        instance._roster   = instance._build_roster()
        instance._circle_context = instance._load_circle_context()
        instance._recent   = instance._build_recent_messages()
        instance._semantic = instance._build_semantic(embedding)
        return instance

    # Private builders — do the work
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
                lines.append(f"- **{row['member_name']}**{alias_str}")
                if row["person_notes"]:
                    lines.append(f"  - {row['person_notes']}")
            lines.append("")
 
        return "\n".join(lines)

    def _build_recent_messages(self) -> str: 
        recent_msgs = self.repo.get_recent_messages(self.circle_id)
        return self._format_recent_messages_context(recent_msgs)

    def _format_recent_messages_context(self, rows: list) -> str:
        if not rows:
            return "## Recent Messages\n_No messages found._\n"
    
        lines = ["## Recent Messages (most recent first)\n"]
        for row in rows:
            ts     = row["sent_at"].strftime("%b %d, %Y %I:%M %p") if row["sent_at"] else "unknown time"
            sender = row["author_name"] or "Unknown"
            lines.append(f"- **{sender}** ({ts}): {row['body']}")
    
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
        return f"## Circle Context: {self.circle_id }\n\n_No context found._\n"

    # Public getters — just return what's cached
    def get_roster(self) -> str: return self._roster
    def get_circle_context(self) -> str: return self._circle_context
    def get_recent_messages(self) -> str: return self._recent
    def get_semantic(self) -> str: return self._semantic

    # Full context for the prompt
    def get_full_context(self) -> str:
        return "\n\n".join([self._roster, self._circle_context, self._recent, self._semantic])

async def ask(question: str, circle_id: str) -> str:
    ctx   = await ContextBuilder.create(circle_id, question)  # sync for now
    response = conversation_chain.invoke({
        "today":           datetime.now().strftime("%B %d, %Y"),
        "circle_context":  ctx.get_circle_context(),
        "roster":          ctx.get_roster(),
        "recent_messages": ctx.get_recent_messages(),
        "semantic_chunks": ctx.get_semantic(),
        "question":        question,
    })
    
    return response.content

async def main():
    circle_id = "6efcc887-98a2-4ce0-b5cb-719a62a80cfd"
    #ctxb = await ContextBuilder.create(circle_id, "who is brining sausage grinders?")
    #print(ctxb.get_roster())
    #print(ctxb.get_circle_context())
    #print(ctxb.get_recent_messages())
    #print(ctxb.get_full_context())
    print(await ask("who is bringing sausage grinders?", circle_id))
    return 0

if __name__ == "__main__":
    import sys
    import asyncio
    sys.exit(asyncio.run(main()))