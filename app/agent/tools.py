"""Tool registry: tools are defined once (JSON schema + async handler) and mapped to any provider."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

if TYPE_CHECKING:
    from app.events import EventBus
    from app.notify.sms import Notifier

log = logging.getLogger(__name__)


class CallControl(Protocol):
    def request_transfer(self, target_uri: str) -> None: ...
    def request_hangup(self) -> None: ...


@dataclass
class ToolContext:
    session_factory: async_sessionmaker
    business_id: int
    conversation_id: int
    caller_phone: str | None
    customer_id: int | None
    call: CallControl
    notifier: "Notifier"
    bus: "EventBus"
    state: dict[str, Any] = field(default_factory=dict)  # per-call scratch space (e.g. draft order id)


Handler = Callable[[ToolContext, dict], Awaitable[dict]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def definitions(self) -> list[Tool]:
        return list(self._tools.values())

    async def run(self, name: str, ctx: ToolContext, args: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool {name!r}."}
        try:
            return await tool.handler(ctx, args)
        except Exception:  # noqa: BLE001 - a tool bug must never drop the call
            log.exception("Tool %s failed", name)
            return {
                "ok": False,
                "error": "Something went wrong on our side. Apologize briefly and try again, "
                         "or offer to connect the caller with a team member.",
            }


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
