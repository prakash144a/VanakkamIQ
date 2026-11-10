"""What every business gets: answer questions, take messages, bring in a person, end the call."""

from sqlalchemy import select

from app.agent.matching import search_faqs
from app.agent.tools import Tool, ToolContext, schema
from app.db.models import Faq
from app.handoff import service as handoff


async def answer_faq(ctx: ToolContext, args: dict) -> dict:
    async with ctx.session_factory() as db:
        faqs = list(await db.scalars(select(Faq).where(Faq.business_id == ctx.business_id)))
    found = search_faqs(faqs, args.get("question", ""))
    if not found:
        return {"ok": True, "answers": [], "note": "No saved answer. Don't guess: offer to connect them with a team member or take a message."}
    return {"ok": True, "answers": [{"question": f.question, "answer": f.answer} for f in found]}


async def transfer_to_human(ctx: ToolContext, args: dict) -> dict:
    return await handoff.start_transfer(ctx, args.get("reason") or "Caller needs a team member")


async def request_callback(ctx: ToolContext, args: dict) -> dict:
    return await handoff.request_callback(ctx, args.get("name"), args.get("phone"), args.get("message") or "")


async def end_call(ctx: ToolContext, args: dict) -> dict:
    ctx.call.request_hangup()
    return {"ok": True, "instruction": "Say a short, warm goodbye now. The call will end right after."}


def core_tools() -> list[Tool]:
    return [
        Tool("answer_faq", "Look up the business's saved answer to a caller's question.",
             schema({"question": {"type": "string"}}, ["question"]), answer_faq),
        Tool("transfer_to_human", "Connect the caller with a team member (see the rules for when).",
             schema({"reason": {"type": "string"}}, ["reason"]), transfer_to_human),
        Tool("request_callback", "Leave a message for the team so someone calls the caller back.",
             schema({
                 "name": {"type": "string"},
                 "phone": {"type": "string", "description": "Only if different from the number they're calling from"},
                 "message": {"type": "string", "description": "What it's about, in a sentence or two"},
             }, ["message"]),
             request_callback),
        Tool("end_call", "End the call after the caller is done and you've said goodbye.", schema({}), end_call),
    ]
