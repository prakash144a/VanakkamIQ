"""One live call: greet, route tool calls, log the transcript, finish cleanly."""

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.tools import ToolContext, ToolRegistry
from app.config import Settings
from app.db.models import Conversation, ConversationEvent, Customer, Handoff, Order
from app.events import EventBus
from app.money import format_money
from app.notify.sms import Notifier
from app.voice.base import (
    AgentTranscript,
    CallerSpeechStarted,
    CallerTranscript,
    Closed,
    PlaybackFinished,
    ProviderError,
    ResponseDone,
    ToolCall,
    VoiceConnection,
)

log = logging.getLogger(__name__)


class CallSession:
    def __init__(
        self,
        *,
        connection: VoiceConnection,
        registry: ToolRegistry,
        session_factory: async_sessionmaker,
        bus: EventBus,
        notifier: Notifier,
        settings: Settings,
        business_id: int,
        conversation_id: int,
        caller_phone: str | None,
        customer_id: int | None,
        greeting_instruction: str,
    ) -> None:
        self.connection = connection
        self.registry = registry
        self.session_factory = session_factory
        self.bus = bus
        self.settings = settings
        self.business_id = business_id
        self.conversation_id = conversation_id
        self.greeting_instruction = greeting_instruction
        self.ctx = ToolContext(
            session_factory=session_factory,
            business_id=business_id,
            conversation_id=conversation_id,
            caller_phone=caller_phone,
            customer_id=customer_id,
            call=self,
            notifier=notifier,
            bus=bus,
        )
        self._pending: tuple[str, str | None] | None = None
        self._action_task: asyncio.Task | None = None
        self._last_agent_text = ""
        self._playback_done = asyncio.Event()

    # --- CallControl (used by tools) ---------------------------------------------------
    def request_transfer(self, target_uri: str) -> None:
        self._pending = ("transfer", target_uri)

    def request_hangup(self) -> None:
        self._pending = ("hangup", None)

    # --- main loop ---------------------------------------------------------------------
    async def run(self) -> None:
        try:
            await self.connection.open()
            await self.connection.request_response(self.greeting_instruction)
            async with asyncio.timeout(self.settings.max_call_seconds):
                async for event in self.connection.events():
                    if isinstance(event, Closed):
                        break
                    await self._handle(event)
        except TimeoutError:
            log.warning("Conversation %s hit the max call length; hanging up", self.conversation_id)
            with contextlib.suppress(Exception):
                await self.connection.hangup()
        except Exception:
            log.exception("Conversation %s crashed", self.conversation_id)
            await self._record("system", "Call handling error; see server logs.")
        finally:
            if self._action_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._action_task, timeout=10)
            with contextlib.suppress(Exception):
                await self.connection.close()
            await self._finish()

    async def _handle(self, event) -> None:
        if isinstance(event, CallerTranscript):
            await self._record("caller", event.text)
        elif isinstance(event, AgentTranscript):
            self._last_agent_text = event.text
            await self._record("agent", event.text)
        elif isinstance(event, PlaybackFinished):
            self._playback_done.set()
        elif isinstance(event, ToolCall):
            result = await self.registry.run(event.name, self.ctx, event.arguments)
            await self._record("tool", None, tool_name=event.name, data={"args": event.arguments, "result": result})
            await self.connection.send_tool_result(event.call_id, result)
        elif isinstance(event, ResponseDone):
            if event.had_tool_calls:
                await self.connection.request_response()
            elif self._pending is not None and self._action_task is None:
                action, self._pending = self._pending, None
                self._playback_done.clear()  # wait for THIS goodbye to finish playing
                self._action_task = asyncio.create_task(self._run_action(*action))
        elif isinstance(event, CallerSpeechStarted):
            pass  # the provider handles barge-in itself on the SIP path
        elif isinstance(event, ProviderError):
            log.warning("Conversation %s provider error: %s", self.conversation_id, event.message)
            await self._record("system", f"Provider error: {event.message}")

    async def _wait_until_heard(self, minimum: float) -> None:
        """Wait until the caller has heard the agent's last words.

        The model finishes *generating* speech well before the caller finishes *hearing* it.
        Prefer the provider's playback-finished signal; otherwise wait for an estimate based
        on how long the last sentence takes to say.
        """
        fallback = max(minimum, speech_seconds(self._last_agent_text))
        try:
            await asyncio.wait_for(self._playback_done.wait(), timeout=fallback)
            await asyncio.sleep(min(0.8, minimum))  # short natural pause after the last word
        except TimeoutError:
            pass

    async def _run_action(self, action: str, target: str | None) -> None:
        if action == "hangup":
            await self._wait_until_heard(self.settings.hangup_delay_seconds)
            with contextlib.suppress(Exception):
                await self.connection.hangup()
            return
        await self._wait_until_heard(self.settings.transfer_delay_seconds)
        try:
            await self.connection.transfer(target or "")
            await self._record("system", f"Call transferred to {target}.")
        except Exception:
            log.exception("Transfer failed for conversation %s", self.conversation_id)
            await self._record("system", f"Transfer to {target} failed.")
            async with self.session_factory() as db:
                handoff = await db.scalar(
                    select(Handoff).where(Handoff.conversation_id == self.conversation_id).order_by(Handoff.id.desc())
                )
                if handoff is not None:
                    handoff.outcome = "failed"
                    await db.commit()
            self._action_task = None
            await self.connection.inject_context(
                "The transfer to a team member did not go through. Apologize, and offer to take a message "
                "with the caller's name and callback number using request_callback."
            )
            await self.connection.request_response()

    # --- persistence -------------------------------------------------------------------
    async def _record(self, role: str, text: str | None, tool_name: str | None = None, data: dict | None = None) -> None:
        async with self.session_factory() as db:
            db.add(ConversationEvent(
                conversation_id=self.conversation_id, role=role, text=text, tool_name=tool_name, data=data
            ))
            await db.commit()
        self.bus.publish(self.business_id, {
            "type": "conversation.event", "conversation_id": self.conversation_id,
            "role": role, "text": text, "tool_name": tool_name,
        })

    async def _finish(self) -> None:
        state = self.ctx.state
        async with self.session_factory() as db:
            convo = await db.get(Conversation, self.conversation_id)
            if convo is None:
                return
            draft = None
            if state.get("order_id"):
                draft = await db.get(Order, state["order_id"])
            unfinished = draft is not None and draft.status == "draft"
            if unfinished:
                draft.status = "cancelled"
            if convo.status != "escalated":
                convo.status = "abandoned" if unfinished and draft.items else "completed"
            convo.ended_at = datetime.now(timezone.utc)
            convo.summary = summarize(convo, draft)

            phone = self.ctx.caller_phone
            if phone:
                customer = await db.scalar(
                    select(Customer).where(Customer.business_id == self.business_id, Customer.phone == phone)
                )
                if customer is None:
                    customer = Customer(business_id=self.business_id, phone=phone)
                    db.add(customer)
                customer.last_call_at = convo.ended_at
                if state.get("customer_name") and not customer.name:
                    customer.name = state["customer_name"]
                await db.flush()
                convo.customer_id = customer.id
            await db.commit()
            status, summary = convo.status, convo.summary
            order_note = ""
            if draft is not None and draft.status not in ("draft", "cancelled"):
                order_note = f", order #{draft.number} ({len(draft.items)} line(s), {format_money(draft.total_cents)})"
            # No names, numbers or transcript text in logs: just the outcome.
            log.info("Conversation %s ended: %s%s, %d events", self.conversation_id, status, order_note, len(convo.events))
        self.bus.publish(self.business_id, {
            "type": "call.ended", "conversation_id": self.conversation_id, "status": status, "summary": summary,
        })


def speech_seconds(text: str) -> float:
    """Rough time to say `text` aloud (about 14 characters per second), plus a little slack."""
    if not text:
        return 0.0
    return min(30.0, len(text) / 14 + 1.0)


def summarize(convo: Conversation, order: Order | None) -> str:
    """Short, deterministic summary for the dashboard. (An LLM summary can replace this later.)"""
    parts: list[str] = []
    if order is not None and order.status not in ("draft", "cancelled"):
        items = ", ".join(f"{i.quantity} x {i.name}" for i in order.items)
        parts.append(f"Order #{order.number} {order.status}: {items}. Total {format_money(order.total_cents)}, {order.fulfillment}.")
    elif order is not None and order.items:
        parts.append("Caller started an order but did not confirm it.")
    if convo.status == "escalated":
        parts.append("Handed to staff.")
    caller_lines = [e.text for e in convo.events if e.role == "caller" and e.text]
    if not parts and caller_lines:
        first = caller_lines[0]
        parts.append(f'Caller said: "{first[:140]}{"..." if len(first) > 140 else ""}"')
    return " ".join(parts) or "No conversation captured."
