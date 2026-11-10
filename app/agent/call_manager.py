"""Accepts incoming calls and keeps track of the live CallSessions in this process."""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.assistant import build_assistant
from app.agent.call_session import CallSession
from app.agent.caller_context import load_caller_context
from app.config import Settings
from app.db.models import Business, Conversation, PhoneNumber
from app.events import EventBus
from app.notify.sms import Notifier
from app.voice.base import VoiceConnection
from app.voice.openai_sip.calls_api import RealtimeCallsAPI
from app.voice.session_config import build_realtime_session
from app.telephony.telnyx_routing import LEG_HEADER
from app.voice.sip_headers import caller_display_name, caller_number, header_values, numbers_in

log = logging.getLogger(__name__)

ConnectionFactory = Callable[[str], VoiceConnection]

SINGLE_BUSINESS = "only active business"


@dataclass
class Route:
    business: Business | None
    dialed_number: str | None = None
    routed_by: str | None = None  # SIP header name, SINGLE_BUSINESS, or None


class CallManager:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker,
        calls_api: RealtimeCallsAPI,
        connection_factory: ConnectionFactory,
        bus: EventBus,
        notifier: Notifier,
        summarizer=None,
        telnyx=None,
    ) -> None:
        self.summarizer = summarizer
        self.telnyx = telnyx  # Voice API client: staff transfers for calls routed through the app
        self.settings = settings
        self.session_factory = session_factory
        self.calls_api = calls_api
        self.connection_factory = connection_factory
        self.bus = bus
        self.notifier = notifier
        self.active: dict[str, asyncio.Task] = {}
        self._seen_webhooks: OrderedDict[str, None] = OrderedDict()

    def already_seen(self, webhook_id: str | None) -> bool:
        """OpenAI may retry a webhook; process each delivery id once."""
        if not webhook_id:
            return False
        if webhook_id in self._seen_webhooks:
            return True
        self._seen_webhooks[webhook_id] = None
        while len(self._seen_webhooks) > 1000:
            self._seen_webhooks.popitem(last=False)
        return False

    async def route_call(self, db, sip_headers: list[dict]) -> Route:
        """Find the business for a call from the dialed number in the SIP headers, and say how."""
        for name in self.settings.sip_called_number_headers:
            for value in header_values(sip_headers, name):
                for number in numbers_in(value):
                    phone = await db.scalar(select(PhoneNumber).where(PhoneNumber.e164 == number))
                    if phone is not None:
                        return Route(await db.get(Business, phone.business_id), number, name)
        if self.settings.single_business_fallback:
            # Telnyx's Translated Number replaces the dialed number with the OpenAI project ID, so
            # the SIP headers may not say which number was called. With exactly one active
            # business that has a number, it must be that one. With two or more, never guess.
            ids = list(await db.scalars(
                select(Business.id).join(PhoneNumber, PhoneNumber.business_id == Business.id)
                .where(Business.is_active.is_(True)).distinct()
            ))
            if len(ids) == 1:
                log.info("Dialed number not in SIP headers; using the only active business (id %s)", ids[0])
                return Route(await db.get(Business, ids[0]), None, SINGLE_BUSINESS)
        return Route(None)

    async def handle_incoming(self, call_id: str, sip_headers: list[dict]) -> CallSession | None:
        if call_id in self.active:
            return None
        log.info("Incoming call %s, SIP headers: %s", call_id, sip_headers)

        async with self.session_factory() as db:
            route = await self.route_call(db, sip_headers)
            business = route.business
            if business is None or not business.is_active:
                log.warning("Call %s: no active business matches the dialed number; rejecting", call_id)
                with contextlib.suppress(Exception):
                    await self.calls_api.reject(call_id, 404)
                return None

            phone = caller_number(sip_headers)
            leg = next(iter(header_values(sip_headers, LEG_HEADER)), None)
            caller = await load_caller_context(db, business.id, phone, caller_display_name(sip_headers))
            assistant = await build_assistant(db, business, caller)
            registry, instructions, greeting = assistant.registry, assistant.instructions, assistant.greeting

            convo = Conversation(
                business_id=business.id, customer_id=caller.customer_id, channel="voice",
                provider="openai_realtime", provider_call_id=call_id, caller_phone=phone,
                dialed_number=route.dialed_number, routed_by=route.routed_by,
            )
            db.add(convo)
            await db.commit()
            business_id, conversation_id = business.id, convo.id
            model = business.model or self.settings.openai_realtime_model
            voice = business.voice

        session_cfg = build_realtime_session(
            model=model, instructions=instructions, voice=voice,
            tools=registry.definitions(), transcription_model=self.settings.openai_transcription_model,
        )
        try:
            await self.calls_api.accept(call_id, session_cfg)
        except Exception:
            log.exception("Call %s: accept failed", call_id)
            async with self.session_factory() as db:
                failed = await db.get(Conversation, conversation_id)
                failed.status = "failed"
                await db.commit()
            return None

        session = CallSession(
            connection=self.connection_factory(call_id), registry=registry,
            session_factory=self.session_factory, bus=self.bus, notifier=self.notifier,
            settings=self.settings, business_id=business_id, conversation_id=conversation_id,
            caller_phone=phone, customer_id=caller.customer_id, greeting_instruction=greeting,
            summarizer=self.summarizer,
            telnyx=self.telnyx if leg else None, telnyx_leg=leg,
        )
        task = asyncio.create_task(session.run(), name=f"call-{call_id}")
        self.active[call_id] = task
        task.add_done_callback(lambda _t: self.active.pop(call_id, None))
        self.bus.publish(business_id, {
            "type": "call.started", "conversation_id": conversation_id, "caller_phone": phone,
            "caller_name": caller.name,
        })
        return session

    async def shutdown(self, grace_seconds: float) -> None:
        """Let live calls finish (e.g. during an Azure revision swap), then cancel the rest."""
        tasks = list(self.active.values())
        if not tasks:
            return
        log.info("Waiting up to %ss for %d active call(s)", grace_seconds, len(tasks))
        _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
