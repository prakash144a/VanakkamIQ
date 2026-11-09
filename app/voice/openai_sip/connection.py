"""Control WebSocket for an OpenAI Realtime SIP call.

Audio flows Telnyx <-> OpenAI directly. This socket carries only events (transcripts,
tool calls) and our commands (tool results, instructions).
"""

import json
import logging
from collections.abc import AsyncIterator

import websockets

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
    VoiceEvent,
)
from app.voice.openai_sip.calls_api import RealtimeCallsAPI

log = logging.getLogger(__name__)


def map_event(event: dict) -> VoiceEvent | None:
    """Translate one Realtime server event into a provider-neutral event (or None to ignore)."""
    etype = event.get("type", "")
    if etype == "conversation.item.input_audio_transcription.completed":
        text = (event.get("transcript") or "").strip()
        return CallerTranscript(text) if text else None
    if etype in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
        text = (event.get("transcript") or "").strip()
        return AgentTranscript(text) if text else None
    if etype == "response.function_call_arguments.done":
        try:
            args = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": event.get("arguments")}
        return ToolCall(call_id=event.get("call_id", ""), name=event.get("name", ""), arguments=args)
    if etype == "response.done":
        output = (event.get("response") or {}).get("output") or []
        return ResponseDone(had_tool_calls=any(item.get("type") == "function_call" for item in output))
    if etype == "input_audio_buffer.speech_started":
        return CallerSpeechStarted()
    if etype == "output_audio_buffer.stopped":
        return PlaybackFinished()
    if etype == "error":
        err = event.get("error") or {}
        message = err.get("message") or json.dumps(err)[:300]
        if err.get("code") == "conversation_already_has_active_response" or "already has an active response" in message:
            # The caller spoke while we asked for a reply; the model is already answering. Not a problem.
            log.debug("Skipped response.create: a response is already in progress")
            return None
        return ProviderError(message)
    return None


class OpenAISipConnection(VoiceConnection):
    def __init__(self, call_id: str, api: RealtimeCallsAPI, api_key: str, ws_url: str) -> None:
        self.call_id = call_id
        self._api = api
        self._api_key = api_key
        self._ws_url = ws_url
        self._ws = None

    async def open(self) -> None:
        self._ws = await websockets.connect(
            f"{self._ws_url}?call_id={self.call_id}",
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=None,
        )

    async def events(self) -> AsyncIterator[VoiceEvent]:
        assert self._ws is not None, "open() first"
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mapped = map_event(event)
                if mapped is not None:
                    yield mapped
        except websockets.ConnectionClosed as exc:
            yield Closed(reason=str(exc))
            return
        yield Closed(reason="socket closed")

    async def _send(self, payload: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            log.info("Call %s: socket already closed, dropping %s", self.call_id, payload.get("type"))

    async def send_tool_result(self, call_id: str, output: dict) -> None:
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(output)},
        })

    async def request_response(self, instructions: str | None = None) -> None:
        payload: dict = {"type": "response.create"}
        if instructions:
            payload["response"] = {"instructions": instructions}
        await self._send(payload)

    async def inject_context(self, text: str) -> None:
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]},
        })

    async def update_instructions(self, instructions: str) -> None:
        await self._send({"type": "session.update", "session": {"type": "realtime", "instructions": instructions}})

    async def transfer(self, target_uri: str) -> None:
        await self._api.refer(self.call_id, target_uri)

    async def hangup(self) -> None:
        await self._api.hangup(self.call_id)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
