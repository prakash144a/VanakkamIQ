"""Delete call transcripts older than each business's retention setting.

Orders, callback requests and the call list (time, number, outcome) are kept; only what was
said is removed. Safe to run on every replica at once: each run just deletes what's due.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Business, Conversation, ConversationEvent

log = logging.getLogger(__name__)
DELETED_NOTE = "Transcript deleted after the retention period."


async def purge_old_transcripts(session_factory: async_sessionmaker, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    removed = 0
    async with session_factory() as db:
        businesses = list(await db.execute(
            select(Business.id, Business.transcript_retention_days).where(Business.transcript_retention_days.is_not(None))
        ))
        for business_id, days in businesses:
            cutoff = now - timedelta(days=days)
            old_calls = (select(Conversation.id).where(
                Conversation.business_id == business_id, Conversation.started_at < cutoff,
                Conversation.status != "active"))
            result = await db.execute(delete(ConversationEvent).where(ConversationEvent.conversation_id.in_(old_calls)))
            removed += result.rowcount or 0
            # Summaries describe what was said (and AI ones can name people), so they go too.
            # Orders and return calls keep their own records.
            await db.execute(update(Conversation).where(
                Conversation.id.in_(old_calls), Conversation.summary.is_not(None),
                Conversation.summary != DELETED_NOTE).values(summary=DELETED_NOTE))
        await db.commit()
    if removed:
        log.info("Deleted %d transcript line(s) past retention", removed)
    return removed


async def retention_loop(session_factory: async_sessionmaker, every_hours: float) -> None:
    while True:
        try:
            await purge_old_transcripts(session_factory)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Transcript retention run failed")
        await asyncio.sleep(every_hours * 3600)
