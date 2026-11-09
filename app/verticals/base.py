"""A Vertical is one business type (restaurant now; receptionist and others later)."""

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.caller_context import CallerContext
from app.agent.tools import ToolRegistry
from app.db.models import Business


class Vertical(Protocol):
    key: str

    def build_registry(self) -> ToolRegistry: ...

    async def build_instructions(self, db: AsyncSession, business: Business, caller: CallerContext) -> str: ...

    def greeting_instruction(self, business: Business, caller: CallerContext) -> str: ...
