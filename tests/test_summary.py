"""AI-written call summaries (7a): used when they work, the basic summary stays when they don't."""

import json

import httpx
import respx
from sqlalchemy import select

from app.agent.summary import Summarizer, _output_text, transcript_text
from app.db.models import Conversation, ConversationEvent
from app.voice.base import CallerTranscript
from tests.test_call_flow import ACCEPT, end_call, start_call

RESPONSES = "https://api.openai.com/v1/responses"


def use_ai(services) -> None:
    services.call_manager.summarizer = Summarizer(services.http, "sk-test", "https://api.openai.com/v1",
                                                  "gpt-4.1-mini", True)


@respx.mock
async def test_ai_summary_replaces_the_basic_one(services):
    use_ai(services)
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    ai = respx.post(RESPONSES).mock(return_value=httpx.Response(200, json={"output": [{
        "type": "message", "content": [{"type": "output_text", "text": "Priya asked about parking and catering;\n a team member should call her back."}]}]}))
    _, task, conn = await start_call(services, "rtc_s1", "+15551230000")
    await conn.push(CallerTranscript("Hi, is there parking? And do you cater for 40 people?"))
    await end_call(conn, task)

    sent = json.loads(ai.calls.last.request.content)
    assert sent["model"] == "gpt-4.1-mini" and sent["store"] is False
    assert "Caller: Hi, is there parking?" in sent["input"] and "Outcome: completed" in sent["input"]
    async with services.session_factory() as db:
        convo = await db.scalar(select(Conversation))
    assert convo.summary == "Priya asked about parking and catering; a team member should call her back."


@respx.mock
async def test_failed_ai_call_keeps_the_basic_summary(services):
    use_ai(services)
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    respx.post(RESPONSES).mock(return_value=httpx.Response(500))
    _, task, conn = await start_call(services, "rtc_s2", "+15551230000")
    await conn.push(CallerTranscript("What time do you close?"))
    await end_call(conn, task)
    async with services.session_factory() as db:
        convo = await db.scalar(select(Conversation))
    assert convo.summary.startswith('Caller said: "What time do you close?')


@respx.mock
async def test_silent_call_makes_no_ai_request(services):
    use_ai(services)
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    ai = respx.post(RESPONSES).mock(return_value=httpx.Response(200, json={"output_text": "x"}))
    _, task, conn = await start_call(services, "rtc_s3", "+15551230000")
    await end_call(conn, task)
    assert not ai.called


def test_transcript_text_and_output_parsing():
    events = [ConversationEvent(role="agent", text="Vanakkam!"), ConversationEvent(role="caller", text="Two dosas"),
              ConversationEvent(role="tool", tool_name="confirm_order", data={"result": {"ok": False}})]
    text = transcript_text(events, ["Outcome: abandoned"])
    assert "Assistant: Vanakkam!" in text and "Caller: Two dosas" in text and "[action confirm_order failed]" in text
    assert _output_text({"output_text": "hello"}) == "hello"
    assert _output_text({"output": [{"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "a"}]}]}) == "a"
