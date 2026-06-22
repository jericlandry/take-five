import asyncio
import logging

from take_five.memory import process_message_for_memory
from take_five.signals import detect_clinical_signals

logger = logging.getLogger(__name__)


async def run_post_storage_pipeline(
    message_id: str,
    circle_id: str,
    body: str,
    sender: str,
    sent_at,
    channel: str,
) -> None:
    """
    Fire-and-forget async tasks that run after every inbound message is stored.
    Called from all channel integrations (GroupMe, SMS, future WhatsApp etc.)
    so each task runs exactly once regardless of channel.

    All tasks are wrapped in create_task so they run concurrently and never
    block the webhook response.
    """
    asyncio.create_task(_run_memory(message_id, circle_id, body, sender, sent_at))
    asyncio.create_task(_run_signal_detection(message_id, circle_id, body, channel))


async def _run_memory(
    message_id: str,
    circle_id: str,
    body: str,
    sender: str,
    sent_at,
) -> None:
    try:
        await process_message_for_memory(
            message_id=message_id,
            circle_id=circle_id,
            body=body,
            sender=sender,
            sent_at=sent_at,
        )
    except Exception as e:
        logger.error(f"[pipeline] Memory processing failed for {message_id}: {e}", exc_info=True)


async def _run_signal_detection(
    message_id: str,
    circle_id: str,
    body: str,
    channel: str,
) -> None:
    try:
        await detect_clinical_signals(
            message_id=message_id,
            circle_id=circle_id,
            body=body,
            channel=channel,
        )
    except Exception as e:
        logger.error(f"[pipeline] Signal detection failed for {message_id}: {e}", exc_info=True)
