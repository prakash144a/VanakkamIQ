"""A scriptable stand-in for the OpenAI control socket."""

import asyncio
import itertools

from app.voice.base import Closed, ToolCall, VoiceConnection


class FakeConnection(VoiceConnection):
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.tool_results: asyncio.Queue = asyncio.Queue()
        self.responses: list[str | None] = []
        self.injected: list[str] = []
        self.transferred_to: str | None = None
        self.hung_up = False
        self.opened = False
        self._ids = itertools.count(1)

    async def open(self) -> None:
        self.opened = True

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if isinstance(event, Closed):
                return

    async def send_tool_result(self, call_id: str, output: dict) -> None:
        await self.tool_results.put((call_id, output))

    async def request_response(self, instructions: str | None = None) -> None:
        self.responses.append(instructions)

    async def inject_context(self, text: str) -> None:
        self.injected.append(text)

    async def update_instructions(self, instructions: str) -> None:
        pass

    async def transfer(self, target_uri: str) -> None:
        self.transferred_to = target_uri

    async def hangup(self) -> None:
        self.hung_up = True

    async def close(self) -> None:
        pass

    # --- test helpers ---
    async def push(self, event) -> None:
        await self.queue.put(event)

    async def wait_until_greeted(self, timeout: float = 5) -> None:
        """The call runs in a background task; wait until it has opened and asked for the greeting."""
        async with asyncio.timeout(timeout):
            while not self.responses:
                await asyncio.sleep(0.01)

    async def call_tool(self, tool: str, /, **arguments) -> dict:
        await self.queue.put(ToolCall(call_id=f"call_{next(self._ids)}", name=tool, arguments=arguments))
        _call_id, output = await asyncio.wait_for(self.tool_results.get(), timeout=5)
        return output
