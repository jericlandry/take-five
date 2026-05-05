# take_five/memory.py
import re
import os
import logging
import asyncio
from datetime import datetime

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_huggingface import HuggingFaceEndpointEmbeddings

logger = logging.getLogger(__name__)

from dotenv import load_dotenv

from take_five.utils import fetch_prompt
from take_five.repository import TakeFiveRepository

load_dotenv()  # Load environment variables from the .env file

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "claude-haiku-4-5-20251001")
EMBEDDINGS_MODEL_NAME = "BAAI/bge-small-en-v1.5"
CHUNK_THRESHOLD = 300
MAX_CHUNK_SIZE = 600

SUMMARY_PROMPT = fetch_prompt('chunk-context-summary')
summary_llm = ChatAnthropic(model=SUMMARY_MODEL, max_tokens=150)

embeddings_model = HuggingFaceEndpointEmbeddings(
    model="BAAI/bge-small-en-v1.5",
    task="feature-extraction",
    # Note: Remote API handles normalization and instructions differently
)

def chunk_message(body: str) -> list[str]:
    if len(body) <= CHUNK_THRESHOLD:
        return [body]
    sentences = re.split(r'(?<=[.!?])\s+', body.strip())
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) > MAX_CHUNK_SIZE:
            if current:
                chunks.append(current.strip())
            current = sentence
        else:
            current += " " + sentence
    if current:
        chunks.append(current.strip())
    return chunks if chunks else [body]

def generate_context_summary(body: str, author: str, sent_at: datetime) -> str:
    date_str = sent_at.strftime("%B %d, %Y")
    prompt = SUMMARY_PROMPT.format(body=body, author=author, date_str=date_str)
    response = summary_llm.invoke([HumanMessage(content=prompt)])

    return response.content.strip()

async def get_embedding(text: str, is_query: bool = False) -> list[float]:
    if is_query:
        # 2. Manually add instruction for queries (standard for BGE v1.5 retrieval)
        instruction = "Represent this sentence for searching relevant passages: "
        embedding = await embeddings_model.aembed_query(instruction + text)
    else:
        # 3. Remote call for documents returns a list of lists
        embeddings = await embeddings_model.aembed_documents([text])
        embedding = embeddings[0] if embeddings else []
        
    return embedding

async def process_message_for_memory(
    message_id: str,
    circle_id: str,
    body: str,
    sender: str,
    sent_at: datetime,
    repo: TakeFiveRepository,
):
    """Chunks + embeds + extracts entities. Fire-and-forget from webhook."""
    try:
        date_str = sent_at.strftime("%B %d, %Y")

        # Run summary and entity extraction (sync LangChain calls)
        context_summary = generate_context_summary(body, sender, sent_at)

        # Chunk and embed
        chunks = chunk_message(body)
        for i, chunk in enumerate(chunks):
            embedded_text = (
                f"Context: {context_summary}\n"
                f"Sender: {sender} | Date: {date_str}\n"
                f"Chunk: {chunk}"
            )
            embedding = await get_embedding(embedded_text)
            repo.upsert_message_chunk(
                message_id=message_id,
                circle_id=circle_id,
                chunk_index=i,
                body=chunk,
                context_header=f"{sender} | {date_str}",
                context_summary=context_summary,
                embedded_text=embedded_text,
                embedding=embedding,
                sent_at=sent_at
            )

        logger.info(
            f"Memory processed: {len(chunks)} chunk(s), "
        )

    except Exception as e:
        logger.error(f"Memory processing failed for message {message_id}: {e}")