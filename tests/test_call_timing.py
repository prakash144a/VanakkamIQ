"""Hanging up only after the caller has heard the goodbye."""

import asyncio

import httpx
import respx

from app.agent.call_session import speech_seconds
from app.voice.base import AgentTranscript, Closed, PlaybackFinished, ResponseDone
from app.voice.openai_sip.connection import map_event
from tests.conftest import sip_headers

ACCEPT = r"https://api\.openai\.com/v1/realtime/calls/.+/accept"


def test_speech_seconds_estimate():
    assert speech_seconds("") == 0
    assert 5 < speech_seconds("x" * 100) < 9
    assert speech_seconds("x" * 10_000) == 30


def test_map_event_playback_and_benign_error():
    assert isinstance(map_event({"type": "output_audio_buffer.stopped"}), PlaybackFinished)
    busy = {"type": "error", "error": {"code": "conversation_already_has_active_response",
                                       "message": "Conversation already has an active response in progress"}}
    assert map_event(busy) is None


@respx.mock
async def test_hangup_waits_for_playback_to_finish(services):
    services.settings.hangup_delay_seconds = 5  # would wait 5s+ without the playback signal
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    await services.call_manager.handle_incoming("rtc_p", sip_headers("+15551230000"))
    task = services.call_manager.active["rtc_p"]
    conn = services.connections[-1]
    await conn.wait_until_greeted()

    await conn.call_tool("end_call")
    await conn.push(ResponseDone(had_tool_calls=True))
    await conn.push(AgentTranscript("Thank you so much, have a wonderful evening! Goodbye!"))
    await conn.push(ResponseDone(had_tool_calls=False))
    await asyncio.sleep(0.2)
    assert not conn.hung_up  # still playing the goodbye

    await conn.push(PlaybackFinished())
    async with asyncio.timeout(3):
        while not conn.hung_up:
            await asyncio.sleep(0.05)
    await conn.push(Closed())
    await asyncio.wait_for(task, 5)
